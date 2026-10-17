import shutil
import subprocess

import pytest


@pytest.fixture(autouse=True)
def clean_proxies(monkeypatch):
    # Test construction must not depend on a host's unrelated proxy settings.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg / ffprobe are required for media integration tests")
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi",
        "-i", "sine=frequency=440:sample_rate=44100", "-t", "9",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-g", "25",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", str(path),
    ], check=True, capture_output=True, timeout=60)
    return path
