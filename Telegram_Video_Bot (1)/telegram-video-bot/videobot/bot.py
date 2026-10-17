import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    MessageEntity,
    Update,
)
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import MB, QUALITIES, Settings
from .errors import ERRORS, JobError
from .security import UnsafeURL, validate_url
from .store import Preferences

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def extract_urls(message: Message) -> list[str]:
    result = []
    entities = message.parse_entities(types=[MessageEntity.URL, MessageEntity.TEXT_LINK])
    entities.update(message.parse_caption_entities(types=[MessageEntity.URL, MessageEntity.TEXT_LINK]))
    for entity, text in entities.items():
        result.append(entity.url if entity.type == MessageEntity.TEXT_LINK else text)
    if not result:
        result = re.findall(r"https?://[^\s<>\"']+", message.text or message.caption or "", re.I)
        result = [value.rstrip(".,;!\u3002") for value in result]
    return list(dict.fromkeys(result))


async def edit_status(message: Message, text: str) -> None:
    # A deleted status message must not cancel the download.
    with contextlib.suppress(TelegramError):
        await message.edit_text(text)


async def terminate_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        if process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
    else:
        # Also terminate ffmpeg/JS descendants if the worker has already exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        if process.returncode is None:
            process.kill()
    await process.wait()


def worker_environment() -> dict[str, str]:
    # The downloader has no reason to inherit bot/cloud/API credentials.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "HOME", "USERPROFILE",
               "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "LANG", "LC_ALL",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "LD_LIBRARY_PATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", YTDLP_NO_PLUGINS="1")
    return env


async def run_worker(settings: Settings, job_dir: Path, status: Message) -> dict:
    kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
              else {"start_new_session": True})
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "videobot.worker", "--job", str(job_dir),
        cwd=ROOT, env=worker_environment(), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, **kwargs,
    )
    started = time.monotonic()
    last_update = 0.0
    last_text = ""
    disk_budget = settings.download_bytes * 5 + settings.upload_bytes * 2
    try:
        while process.returncode is None:
            if time.monotonic() - started > settings.timeout:
                raise JobError("timeout")
            if time.monotonic() - last_update >= 5:
                last_update = time.monotonic()
                from .worker import directory_size
                if await asyncio.to_thread(directory_size, job_dir) > disk_budget:
                    raise JobError("size")
                try:
                    state = json.loads((job_dir / "progress.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                stage = state.get("stage")
                text = {
                    "extracting": "🔎 Получаю информацию о видео…\n/cancel — отменить",
                    "downloading": "⬇️ Скачиваю видео" + (
                        f": {state['percent']}%" if state.get("percent") is not None else "…"
                    ) + "\n/cancel — отменить",
                    "processing": "🎬 Подготавливаю видео…\n/cancel — отменить",
                    "splitting": "✂️ Деление на части для Telegram…\n/cancel — отменить",
                }.get(stage, "⏳ Обрабатываю ссылку…\n/cancel — отменить")
                if text != last_text:
                    await edit_status(status, text)
                    last_text = text
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                pass
        try:
            result = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise JobError("internal") from exc
        if not result.get("ok"):
            raise JobError(result.get("error", "internal"))
        return result
    finally:
        await terminate_tree(process)


@dataclass
class Job:
    user_id: int
    chat_id: int
    url: str
    quality: str
    status: Message
    task: asyncio.Task | None = None
    running: bool = False


class VideoBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads = settings.data_dir / "downloads"
        self.downloads.mkdir(exist_ok=True)
        self.preferences = Preferences(settings.data_dir / "preferences.sqlite3")
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.jobs: dict[int, Job] = {}
        self.last_request: dict[int, float] = {}

    async def authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user and (not self.settings.allowed_ids or user.id in self.settings.allowed_ids):
            return True
        if update.callback_query:
            await update.callback_query.answer("Нет доступа к этому боту.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Этот бот доступен только выбранным пользователям.")
        return False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        await update.effective_message.reply_text(
            "🎬 Отправь ссылку на видео — я попробую скачать его и пришлю сюда.\n\n"
            "TikTok · Pinterest · YouTube · Instagram · VK · Rutube · Facebook "
            "и другие сайты, поддерживаемые yt-dlp.\n\n"
            "Нужна ссылка на один ролик. Большие видео отправляются частями. "
            "Некоторые сайты требуют авторизацию или ограничивают скачивание.\n\n"
            "/quality — качество\n/cancel — отмена\n/status — состояние\n"
            "/id — мой ID\n/help — возможности и лимиты"
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        settings = self.settings
        await update.effective_message.reply_text(
            "Пришли одну http(s) ссылку на страницу с видео. Можно вставить её вместе с текстом. "
            "Плейлисты и прямые эфиры не загружаются.\n\n"
            f"Лимит загрузки: {settings.download_bytes // MB} МБ. "
            f"Длительность: до {settings.duration // 60} мин. "
            f"Одна отправляемая часть: до {settings.upload_bytes // MB} МБ. "
            f"До {settings.max_parts} частей.\n\n"
            "Качество — верхняя граница: если у источника оно ниже, придёт доступное. "
            "Выбор сохраняется через /quality.\n\n"
            "Закрытый контент требует доступа, а DRM не поддерживается. "
            "Поддержка сайтов зависит от yt-dlp, региона сервера и правил площадки."
        )

    async def identity(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user:
            await update.effective_message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")

    async def quality(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        user_id = update.effective_user.id
        if context.args:
            choice = context.args[0].lower()
            if choice not in QUALITIES:
                await update.effective_message.reply_text("Пример: /quality 720. Варианты: 360, 480, 720, 1080, best.")
                return
            self.preferences.set_quality(user_id, choice)
            await update.effective_message.reply_text(f"Качество для следующих загрузок: {choice}.")
            return
        current = self.preferences.get_quality(user_id, self.settings.quality)
        buttons = [InlineKeyboardButton(
            ("✅ " if value == current else "") + ("Максимум" if value == "best" else value + "p"),
            callback_data=f"quality:{value}",
        ) for value in QUALITIES]
        await update.effective_message.reply_text(
            f"Качество для следующих видео: {current}. Выбери вариант:",
            reply_markup=InlineKeyboardMarkup([buttons[:3], buttons[3:]]),
        )

    async def quality_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        choice = update.callback_query.data.split(":", 1)[-1]
        if choice not in QUALITIES:
            await update.callback_query.answer("Неизвестный вариант.")
            return
        self.preferences.set_quality(update.effective_user.id, choice)
        await update.callback_query.answer(f"Выбрано: {choice}")
        with contextlib.suppress(TelegramError):
            await update.callback_query.edit_message_text(f"Качество для следующих видео: {choice}.")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        job = self.jobs.get(update.effective_user.id)
        if job:
            text = "Твоё видео обрабатывается. /cancel — отменить." if job.running else "Твоя ссылка в очереди. /cancel — отменить."
        else:
            text = "Активных загрузок нет. Пришли ссылку на видео."
        await update.effective_message.reply_text(text)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        job = self.jobs.get(update.effective_user.id)
        if not job or job.task is None:
            await update.effective_message.reply_text("Активных загрузок нет.")
            return
        job.task.cancel()
        # Let job cleanup finish before accepting another request by this user.
        await asyncio.gather(job.task, return_exceptions=True)
        self.jobs.pop(job.user_id, None)
        await update.effective_message.reply_text("Загрузка отменена.")

    async def receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.authorized(update):
            return
        message = update.effective_message
        urls = extract_urls(message)
        if len(urls) != 1:
            await message.reply_text("Пришли одну ссылку на видео в одном сообщении.")
            return
        try:
            url = validate_url(urls[0])
        except UnsafeURL as exc:
            await message.reply_text(str(exc))
            return
        user_id = update.effective_user.id
        if user_id in self.jobs:
            await message.reply_text("Сначала дождись своей текущей загрузки или нажми /cancel.")
            return
        if len(self.jobs) >= self.settings.concurrency + self.settings.queue_size:
            await message.reply_text("Очередь заполнена. Попробуй позже.")
            return
        now = time.monotonic()
        if now - self.last_request.get(user_id, -self.settings.cooldown) < self.settings.cooldown:
            await message.reply_text("Подожди несколько секунд перед следующей ссылкой.")
            return
        self.last_request[user_id] = now
        # Bound this cooldown-only map; actual jobs remain tracked separately.
        if len(self.last_request) > 10_000:
            self.last_request = {key: value for key, value in self.last_request.items()
                                 if now - value < self.settings.cooldown}
        status = await message.reply_text("⏳ Ссылка принята. Ожидаю свободное место…\n/cancel — отменить")
        job = Job(user_id, message.chat_id, url,
                  self.preferences.get_quality(user_id, self.settings.quality), status)
        self.jobs[user_id] = job
        # These tasks are managed here (not Application.create_task), so shutdown
        # can cancel pending downloads before waiting for them indefinitely.
        job.task = asyncio.create_task(self.process(job, context.application),
                                       name=f"video-job-{user_id}")

    async def process(self, job: Job, application: Application) -> None:
        try:
            async with self.semaphore:
                job.running = True
                async with asyncio.timeout(self.settings.timeout):
                    with tempfile.TemporaryDirectory(prefix="job-", dir=self.downloads) as temp:
                        job_dir = Path(temp)
                        free = shutil.disk_usage(self.downloads).free
                        if free < self.settings.download_bytes * 5 + 100 * MB:
                            raise JobError("storage")
                        request = {
                            "url": job.url, "quality": job.quality,
                            "max_download_bytes": self.settings.download_bytes,
                            "upload_limit": self.settings.upload_bytes,
                            "max_duration": self.settings.duration,
                            "max_parts": self.settings.max_parts,
                            "cookies_file": str(self.settings.cookies) if self.settings.cookies else None,
                        }
                        (job_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
                        result = await run_worker(self.settings, job_dir, job.status)
                        total = len(result["parts"])
                        for index, part in enumerate(result["parts"], 1):
                            path = await asyncio.to_thread(Path(part["path"]).resolve)
                            if not path.is_relative_to(job_dir) or path.is_symlink() or not path.is_file():
                                raise JobError("internal")
                            if path.stat().st_size > self.settings.upload_bytes:
                                raise JobError("size")
                            await edit_status(job.status, f"📤 Отправляю видео: {index}/{total}…\n/cancel — отменить")
                            caption = result["title"]
                            if total > 1:
                                caption += f"\nЧасть {index} из {total}"
                            await self.send_part(application, job, path, part, caption)
                            if index < total:
                                await asyncio.sleep(1.2)
                        await edit_status(job.status, "✅ Готово. Пришли следующую ссылку.")
        except asyncio.CancelledError:
            await edit_status(job.status, "Загрузка отменена.")
            raise
        except (JobError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, JobError) else "timeout"
            await edit_status(job.status, "⚠️ " + ERRORS.get(code, ERRORS["internal"]))
            log.info("Job finished with code %s", code)
        except TelegramError as exc:
            # Don't log Telegram request URLs; they contain the bot token.
            log.warning("Telegram delivery failed: %s", type(exc).__name__)
            await edit_status(job.status, "Не удалось завершить отправку в Telegram. "
                              "Если часть видео уже пришла, она останется в чате.")
        except Exception as exc:
            log.error("Job failed: %s", type(exc).__name__)
            await edit_status(job.status, ERRORS["internal"])
        finally:
            if self.jobs.get(job.user_id) is job:
                self.jobs.pop(job.user_id, None)

    async def send_part(self, application: Application, job: Job, path: Path,
                        part: dict, caption: str) -> None:
        for attempt in range(3):
            try:
                with path.open("rb") as stream:
                    await application.bot.send_video(
                        chat_id=job.chat_id,
                        video=InputFile(stream, filename=path.name, read_file_handle=False),
                        caption=caption, duration=part["duration"],
                        width=part["width"], height=part["height"], supports_streaming=True,
                        read_timeout=120, write_timeout=600, connect_timeout=30,
                    )
                return
            except RetryAfter as exc:
                if attempt == 2:
                    raise
                delay = exc.retry_after
                seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
                if seconds > 60:
                    raise
                await asyncio.sleep(seconds + 0.5)
            except BadRequest as exc:
                # Fall back only for a definite format rejection, never for a
                # timeout (which could mean Telegram already delivered a video).
                if not any(word in str(exc).lower() for word in ("video_content_type", "video file", "video format")):
                    raise
                with path.open("rb") as stream:
                    await application.bot.send_document(
                        chat_id=job.chat_id,
                        document=InputFile(stream, filename=path.name, read_file_handle=False),
                        caption=caption, read_timeout=120, write_timeout=600,
                    )
                return

    async def on_init(self, application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("start", "Начать"), BotCommand("quality", "Качество видео"),
            BotCommand("cancel", "Отменить загрузку"), BotCommand("status", "Состояние загрузки"),
            BotCommand("id", "Мой Telegram ID"), BotCommand("help", "Помощь"),
        ])
        # Remove only abandoned job folders from previous interrupted runs.
        cutoff = time.time() - 86_400
        for path in self.downloads.glob("job-*"):
            if path.is_dir() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        log.info("Bot is ready; downloads=%s, queue=%s", self.settings.concurrency, self.settings.queue_size)

    async def on_stop(self, application: Application) -> None:
        tasks = [job.task for job in self.jobs.values() if job.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Update failed: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message:
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text("Не удалось обработать запрос. Попробуй ещё раз.")


def build_application(settings: Settings) -> Application:
    service = VideoBot(settings)
    builder = (Application.builder().token(settings.token).concurrent_updates(False)
               .connection_pool_size(16).connect_timeout(30).read_timeout(60)
               .media_write_timeout(600).post_init(service.on_init).post_stop(service.on_stop))
    if settings.api_url:
        builder = (builder.base_url(settings.api_url + "/bot")
                   .base_file_url(settings.api_url + "/file/bot"))
    application = builder.build()
    application.bot_data["video_service"] = service
    for command, callback in (("start", service.start), ("help", service.help),
                              ("quality", service.quality), ("cancel", service.cancel),
                              ("status", service.status), ("id", service.identity)):
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(service.quality_callback, pattern=r"^quality:"))
    application.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                                            service.receive))
    application.add_error_handler(on_error)
    return application
