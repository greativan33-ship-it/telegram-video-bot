import json
import os
import socket
import subprocess
import sys

import pytest

from videobot.bot import worker_environment
from videobot.config import Settings
from videobot.errors import JobError
from videobot.security import UnsafeURL, public_ip, validate_url
from videobot.store import Preferences
from videobot.worker import check_info

TOKEN = "123456789:" + "a" * 30


def test_valid_settings_and_secret_redaction(tmp_path):
    settings = Settings.from_env({"BOT_TOKEN": TOKEN, "DATA_DIR": str(tmp_path)})
    assert settings.upload_bytes == 49_000_000
    assert TOKEN not in repr(settings)
    assert settings.quality == "720"


@pytest.mark.parametrize("patch", [
    {"BOT_TOKEN": ""}, {"MAX_CONCURRENT_DOWNLOADS": "0"},
    {"MAX_UPLOAD_MB": "50"}, {"DEFAULT_QUALITY": "1440"},
    {"ALLOWED_USER_IDS": "1,abc"}, {"ALLOWED_USER_IDS": "-1"},
    {"LOCAL_BOT_API_URL": "http://host:8081/bot"}, {"COOKIES_FILE": "/missing/cookies.txt"},
])
def test_invalid_settings(patch):
    with pytest.raises(ValueError):
        Settings.from_env({"BOT_TOKEN": TOKEN, **patch})


def test_local_api_allows_large_files():
    settings = Settings.from_env({"BOT_TOKEN": TOKEN, "LOCAL_BOT_API_URL": "http://127.0.0.1:8081",
                                  "MAX_UPLOAD_MB": "1950"})
    assert settings.upload_bytes == 1_950_000_000


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a", "http://127.1/a", "http://2130706433/a", "http://0x7f000001/a",
    "http://10.0.0.1/a", "http://169.254.169.254/latest", "http://192.168.1.1",
    "http://[::1]/a", "http://[::ffff:127.0.0.1]/", "http://localhost/a",
    "http://something.local/", "http://secret.internal/", "file:///etc/passwd",
    "ftp://example.com/video", "https://user:pass@example.com/a",
    "https://example.com:8080/a", "https://example.com:bad/a", "https://example.com/\nsecret",
    "https://example.com\\@localhost/a", "not a url",
])
def test_nonpublic_and_invalid_urls_rejected(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_public_links_and_fragments():
    assert validate_url("https://youtu.be/abc?t=2#test") == "https://youtu.be/abc?t=2"
    assert validate_url("https://vkvideo.ru/video-123_456") == "https://vkvideo.ru/video-123_456"
    assert public_ip("8.8.8.8")
    assert not public_ip("224.0.0.1")
    assert not public_ip("::ffff:10.0.0.1")


def test_dns_resolution_to_private_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.2.3.4", 443))])
    with pytest.raises(UnsafeURL):
        validate_url("https://public-looking.example.com/video", resolve=True)


def test_socket_audit_blocks_actual_private_connect():
    result = subprocess.run([sys.executable, "-c", """
import socket
from videobot.security import install_network_guard
install_network_guard()
try:
    socket.create_connection(('127.0.0.1', 80), timeout=1)
except OSError as error:
    assert 'Blocked non-public' in str(error)
else:
    raise AssertionError('private connect was not blocked')
"""], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_worker_returns_safe_error_in_real_subprocess(tmp_path):
    (tmp_path / "request.json").write_text(json.dumps({"url": "http://127.0.0.1/test"}))
    result = subprocess.run([sys.executable, "-m", "videobot.worker", "--job", str(tmp_path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert json.loads((tmp_path / "result.json").read_text()) == {"ok": False, "error": "unsafe"}
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize(("info", "code"), [
    ({"_type": "playlist", "entries": []}, "playlist"),
    ({"is_live": True}, "live"), ({"has_drm": True}, "drm"),
    ({"duration": 4000}, "duration"), ({"filesize": 600_000_000}, "size"),
])
def test_pre_download_limits(info, code):
    with pytest.raises(JobError) as error:
        check_info(info, {"max_duration": 3600, "max_download_bytes": 500_000_000})
    assert error.value.code == code


def test_preferences_survive_reopen(tmp_path):
    path = tmp_path / "preferences.sqlite3"
    Preferences(path).set_quality(123, "1080")
    assert Preferences(path).get_quality(123, "720") == "1080"
    assert Preferences(path).get_quality(456, "480") == "480"


def test_worker_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("HTTP_PROXY", "http://secret-proxy")
    env = worker_environment()
    assert "BOT_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert "HTTP_PROXY" not in env
    assert env["YTDLP_NO_PLUGINS"] == "1"
    assert env.get("PATH") == os.environ.get("PATH")
