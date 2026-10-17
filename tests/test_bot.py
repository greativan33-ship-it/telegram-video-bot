import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Message

import videobot.bot as bot_module
from videobot.bot import VideoBot, build_application, extract_urls
from videobot.config import Settings


def settings(tmp_path, **extra):
    return Settings.from_env({"BOT_TOKEN": "123456:" + "a" * 30,
                              "DATA_DIR": str(tmp_path), "COOLDOWN_SECONDS": "0",
                              "MAX_DOWNLOAD_MB": "1", **extra})


def make_message(text, **extra):
    return Message.de_json({"message_id": 1, "date": 1,
                            "chat": {"id": 42, "type": "private"}, "text": text, **extra}, None)


def test_extracts_url_with_utf16_emoji_offset():
    prefix = "🎬 Смотри "
    url = "https://youtu.be/example?t=3"
    message = make_message(prefix + url, entities=[{
        "type": "url", "offset": len(prefix.encode("utf-16-le")) // 2, "length": len(url),
    }])
    assert extract_urls(message) == [url]


def test_caption_and_hidden_links():
    message = make_message("Скачать", entities=[{
        "type": "text_link", "offset": 0, "length": 7, "url": "https://pin.it/example",
    }])
    assert extract_urls(message) == ["https://pin.it/example"]
    caption = Message.de_json({"message_id": 2, "date": 1, "chat": {"id": 42, "type": "private"},
                               "caption": "https://rutube.ru/video/example"}, None)
    assert extract_urls(caption) == ["https://rutube.ru/video/example"]


def test_distinct_urls_are_not_silently_ignored():
    assert len(extract_urls(make_message("https://youtu.be/one https://youtu.be/two"))) == 2


def test_application_builds_with_registered_handlers(tmp_path):
    application = build_application(settings(tmp_path))
    assert len(application.handlers[0]) == 8
    assert isinstance(application.bot_data["video_service"], VideoBot)


def stub_update(user_id):
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(
        text="https://youtu.be/example", caption=None, chat_id=user_id,
        parse_entities=lambda **kw: {}, parse_caption_entities=lambda **kw: {},
        reply_text=AsyncMock(return_value=status),
    )
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id),
                           effective_message=message, callback_query=None), status


def test_queue_capacity_concurrency_and_duplicate_user(tmp_path, monkeypatch):
    async def scenario():
        service = VideoBot(settings(tmp_path, MAX_CONCURRENT_DOWNLOADS="1", MAX_QUEUE_SIZE="1"))
        gate = asyncio.Event()
        started = []
        context = SimpleNamespace(application=SimpleNamespace())

        async def fake_worker(config, job_dir, status):
            started.append(status)
            await gate.wait()
            path = job_dir / "video.mp4"
            path.write_bytes(b"fixture")
            return {"title": "Тест", "parts": [{"path": str(path), "duration": 1,
                                               "width": 320, "height": 240}]}

        monkeypatch.setattr(bot_module, "run_worker", fake_worker)
        service.send_part = AsyncMock()
        one, status_one = stub_update(1)
        two, status_two = stub_update(2)
        three, _ = stub_update(3)
        try:
            await service.receive(one, context)
            await asyncio.sleep(0)
            await service.receive(two, context)
            await asyncio.sleep(0)
            assert started == [status_one]
            await service.receive(three, context)
            assert "Очередь заполнена" in three.effective_message.reply_text.call_args.args[0]
            await service.receive(one, context)
            assert "текущей загрузки" in one.effective_message.reply_text.call_args.args[0]
            tasks = [job.task for job in service.jobs.values()]
            gate.set()
            await asyncio.gather(*tasks)
            assert started == [status_one, status_two]
            assert service.send_part.await_count == 2
            assert not service.jobs
            assert not list(service.downloads.iterdir())
        finally:
            await service.on_stop(context.application)

    asyncio.run(scenario())


def test_cancel_running_and_waiting_jobs_cleans_temporary_files(tmp_path, monkeypatch):
    async def scenario():
        service = VideoBot(settings(tmp_path, MAX_CONCURRENT_DOWNLOADS="1", MAX_QUEUE_SIZE="2"))
        entered = asyncio.Event()
        stopped = asyncio.Event()
        context = SimpleNamespace(application=SimpleNamespace())

        async def fake_worker(config, job_dir, status):
            (job_dir / "partial.mp4").write_bytes(b"incomplete")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(bot_module, "run_worker", fake_worker)
        one, _ = stub_update(1)
        two, _ = stub_update(2)
        await service.receive(one, context)
        await asyncio.wait_for(entered.wait(), 2)
        await service.receive(two, context)
        await service.cancel(two, context)
        assert 2 not in service.jobs
        await service.cancel(one, context)
        assert stopped.is_set()
        assert not service.jobs
        assert not list(service.downloads.iterdir())

    asyncio.run(scenario())


def test_access_restriction_before_download(tmp_path):
    async def scenario():
        service = VideoBot(settings(tmp_path, ALLOWED_USER_IDS="1"))
        update, _ = stub_update(2)
        await service.receive(update, SimpleNamespace(application=None))
        assert not service.jobs
        assert "доступен только" in update.effective_message.reply_text.call_args.args[0]

    asyncio.run(scenario())
