import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

QUALITIES = ("360", "480", "720", "1080", "best")
MB = 1_000_000


def number(env: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(env.get(key, default))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{key}: требуется целое число.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key}: укажите значение от {minimum} до {maximum}.")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    allowed_ids: frozenset[int]
    quality: str
    concurrency: int
    queue_size: int
    download_bytes: int
    upload_bytes: int
    duration: int
    max_parts: int
    timeout: int
    cooldown: int
    data_dir: Path
    cookies: Path | None
    api_url: str

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = dict(os.environ) if env is None else env
        token = env.get("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Заполните BOT_TOKEN в файле .env токеном из @BotFather.")
        try:
            allowed_ids = frozenset(
                int(item.strip()) for item in env.get("ALLOWED_USER_IDS", "").split(",")
                if item.strip()
            )
            if any(item <= 0 for item in allowed_ids):
                raise ValueError
        except ValueError as exc:
            raise ValueError("ALLOWED_USER_IDS: нужны положительные числовые ID через запятую.") from exc
        quality = env.get("DEFAULT_QUALITY", "720").lower().strip()
        if quality not in QUALITIES:
            raise ValueError("DEFAULT_QUALITY: 360, 480, 720, 1080 или best.")
        api_url = env.get("LOCAL_BOT_API_URL", "").rstrip("/")
        if api_url:
            parts = urlsplit(api_url)
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.path or parts.query or parts.fragment or parts.username):
                raise ValueError("LOCAL_BOT_API_URL: нужен корневой http(s) URL без пути и пароля.")
        cookies_raw = env.get("COOKIES_FILE", "").strip()
        cookies = Path(cookies_raw).expanduser().resolve() if cookies_raw else None
        if cookies and not cookies.is_file():
            raise ValueError("COOKIES_FILE: файл cookies не найден.")
        return cls(
            token=token, allowed_ids=allowed_ids, quality=quality,
            concurrency=number(env, "MAX_CONCURRENT_DOWNLOADS", 2, 1, 8),
            queue_size=number(env, "MAX_QUEUE_SIZE", 10, 0, 100),
            download_bytes=number(env, "MAX_DOWNLOAD_MB", 500, 1, 10_000) * MB,
            upload_bytes=number(env, "MAX_UPLOAD_MB", 49, 1, 1950 if api_url else 49) * MB,
            duration=number(env, "MAX_DURATION_SECONDS", 3600, 10, 86_400),
            max_parts=number(env, "MAX_PARTS", 30, 1, 100),
            timeout=number(env, "JOB_TIMEOUT_SECONDS", 1800, 30, 14_400),
            cooldown=number(env, "COOLDOWN_SECONDS", 5, 0, 3600),
            data_dir=Path(env.get("DATA_DIR", "data")).expanduser().resolve(),
            cookies=cookies, api_url=api_url,
        )
