import io
import socket
import subprocess
from pathlib import Path

import pytest
from yt_dlp.networking import Response

from videobot.errors import JobError
from videobot.media import prepare_video, probe
from videobot.worker import PublicYoutubeDL, download


def test_small_mp4_keeps_original(sample_video, tmp_path):
    parts = prepare_video(sample_video, tmp_path / "out", upload_limit=49_000_000,
                          max_parts=30, max_duration=3600)
    assert len(parts) == 1
    assert Path(parts[0]["path"]) == sample_video
    assert parts[0]["width"] == 320


def test_splits_playable_video_within_size_limit(sample_video, tmp_path):
    limit = 180_000
    assert sample_video.stat().st_size > limit
    parts = prepare_video(sample_video, tmp_path / "out", upload_limit=limit,
                          max_parts=30, max_duration=3600)
    assert len(parts) > 1
    actual = [probe(Path(part["path"])) for part in parts]
    assert all(0 < Path(part["path"]).stat().st_size <= limit for part in parts)
    assert all(item.video_codec == "h264" and item.audio_codec == "aac" for item in actual)
    assert abs(sum(item.duration for item in actual) - probe(sample_video).duration) < 0.6


def test_part_limit_and_duration_limit(sample_video, tmp_path):
    with pytest.raises(JobError) as error:
        prepare_video(sample_video, tmp_path / "many", upload_limit=50_000,
                      max_parts=1, max_duration=3600)
    assert error.value.code == "parts"
    with pytest.raises(JobError) as error:
        prepare_video(sample_video, tmp_path / "long", upload_limit=49_000_000,
                      max_parts=30, max_duration=1)
    assert error.value.code == "duration"


def test_long_keyframe_interval_fallback_still_fits(sample_video, tmp_path):
    source = tmp_path / "long-gop.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(sample_video),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "13",
                    "-g", "9999", "-keyint_min", "9999", "-sc_threshold", "0",
                    "-c:a", "copy", str(source)], check=True, capture_output=True, timeout=60)
    limit = 180_000
    assert source.stat().st_size > limit
    parts = prepare_video(source, tmp_path / "out", upload_limit=limit,
                          max_parts=30, max_duration=3600)
    assert len(parts) > 1
    assert any("fixed" in part["path"] for part in parts)
    assert all(Path(part["path"]).stat().st_size <= limit for part in parts)
    assert abs(sum(probe(Path(part["path"])).duration for part in parts) - probe(source).duration) < 0.7


def test_incompatible_video_codec_is_converted(sample_video, tmp_path):
    source = tmp_path / "mpeg4.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(sample_video),
                    "-t", "1.5", "-an", "-c:v", "mpeg4", "-q:v", "4", str(source)],
                   check=True, capture_output=True, timeout=30)
    assert probe(source).video_codec == "mpeg4"
    parts = prepare_video(source, tmp_path / "out", upload_limit=49_000_000,
                          max_parts=30, max_duration=3600)
    assert probe(Path(parts[0]["path"])).video_codec == "h264"


def test_real_yt_dlp_pipeline_with_simulated_cdn(sample_video, tmp_path, monkeypatch):
    # Real yt-dlp generic extractor + HTTP downloader + FFmpeg probing.
    # Only the CDN transport is replaced; no internet or account is required.
    payload = sample_video.read_bytes()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])

    def mock_urlopen(self, request):
        url = request if isinstance(request, str) else request.url
        assert url == "https://cdn.example.com/demo.mp4"
        return Response(io.BytesIO(payload), url, {"Content-Type": "video/mp4",
                         "Content-Length": str(len(payload))}, status=200)

    monkeypatch.setattr(PublicYoutubeDL, "urlopen", mock_urlopen)
    result = download({"url": "https://cdn.example.com/demo.mp4", "quality": "720",
                       "max_download_bytes": 10_000_000, "upload_limit": 49_000_000,
                       "max_parts": 30, "max_duration": 3600}, tmp_path)
    assert result["ok"] is True
    assert len(result["parts"]) == 1
    assert probe(Path(result["parts"][0]["path"])).audio_codec == "aac"
