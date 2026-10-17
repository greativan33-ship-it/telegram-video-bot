import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
from importlib.metadata import version

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.error import TelegramError

from .bot import build_application
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Бот для скачивания видео по ссылке")
    parser.add_argument("--check", action="store_true", help="Проверить настройки без подключения к Telegram")
    parser.add_argument("--logout-cloud", action="store_true",
                        help="Отключить облачный Bot API перед переходом на локальный сервер")
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
        if missing:
            raise ValueError("Не найдены " + ", ".join(missing) + ". Установите FFmpeg; инструкция в README.md.")
    except ValueError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        sys.exit(2)
    if args.logout_cloud:
        if not settings.api_url:
            print("Сначала задайте LOCAL_BOT_API_URL. Команда нужна только для перехода на свой Bot API.")
            sys.exit(2)

        async def logout():
            async with Bot(settings.token) as cloud_bot:
                await cloud_bot.log_out()

        try:
            asyncio.run(logout())
        except TelegramError as exc:
            print(f"Не удалось отключить облачный Bot API: {type(exc).__name__}", file=sys.stderr)
            sys.exit(1)
        print("Облачный Bot API отключён. Теперь запускайте бота с LOCAL_BOT_API_URL.")
        return
    if not any(shutil.which(name) for name in ("deno", "node")):
        logging.warning("Для YouTube установите Deno или Node.js: см. README.md.")
    if not settings.allowed_ids:
        logging.info("Доступ открыт всем. Для личного бота задайте ALLOWED_USER_IDS.")
    if args.check:
        print("Настройки корректны. Токен проверен по формату; запросы в Telegram не отправлялись.")
        print(f"Python {sys.version.split()[0]}, python-telegram-bot {version('python-telegram-bot')}, "
              f"yt-dlp {version('yt-dlp')}")
        print("FFmpeg и ffprobe найдены. JavaScript runtime: " +
              ("Deno" if shutil.which("deno") else "Node.js" if shutil.which("node") else "не найден"))
        return
    try:
        # On Windows asyncio subprocesses require Proactor (the Python default).
        application = build_application(settings)
        application.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
                                drop_pending_updates=True,
                                stop_signals=None if os.name == "nt" else (signal.SIGINT, signal.SIGTERM))
    except (TelegramError, RuntimeError) as exc:
        print(f"Ошибка Telegram: {type(exc).__name__}. Проверьте токен, сеть и отсутствие второй копии бота.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
