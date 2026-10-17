"""One isolated child process per job. Does not receive the Telegram token."""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import yt_dlp
from yt_dlp.downloader.external import FFmpegFD
from yt_dlp.networking import Request
from yt_dlp.utils import DownloadError

from .errors import JobError, classify_error
from .media import prepare_video
from .security import UnsafeURL, install_network_guard, validate_url


def block_external_download(self, filename, info_dict):
    # Postprocessing is allowed, but FFmpeg must never fetch a remote manifest.
    # Some HLS manifests silently make yt-dlp fall back from native HLS to it.
    raise JobError("unsupported")


def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            pass
    return total


class QuietLogger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def check_info(info: dict, request: dict) -> None:
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        raise JobError("playlist")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise JobError("live")
    if info.get("has_drm"):
        raise JobError("drm")
    if (info.get("duration") or 0) > request["max_duration"]:
        raise JobError("duration")
    if (info.get("filesize") or 0) > request["max_download_bytes"]:
        raise JobError("size")


class PublicYoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, options, request):
        self.job_request = request
        super().__init__(options)
        # Native C networking bypasses Python's socket audit hooks. Only use
        # standard Python handlers. No external yt-dlp plugins are loaded.
        director = self._request_director
        for key in list(director.handlers):
            if key not in ("Urllib", "Requests", "Websockets"):
                director.handlers.pop(key).close()

    def urlopen(self, req):
        url = req if isinstance(req, str) else (
            req.url if isinstance(req, Request) else req.get_full_url())
        validate_url(url, resolve=True)
        return super().urlopen(req)

    def process_ie_result(self, ie_result, download=True, extra_info=None):
        check_info(ie_result, self.job_request)
        return super().process_ie_result(ie_result, download, extra_info)

    def process_info(self, info_dict):
        check_info(info_dict, self.job_request)
        formats = info_dict.get("requested_formats") or [info_dict]
        if sum(item.get("filesize") or 0 for item in formats) > self.job_request["max_download_bytes"]:
            raise JobError("size")
        for item in formats:
            protocol = item.get("protocol") or "https"
            if protocol not in ("http", "https", "m3u8", "m3u8_native", "http_dash_segments"):
                raise JobError("unsupported")
            if item.get("url"):
                validate_url(item["url"], resolve=True)
        return super().process_info(info_dict)


def download(request: dict, job_dir: Path) -> dict:
    url = validate_url(request["url"], resolve=True)
    download_dir = job_dir / "download"
    download_dir.mkdir()
    progress_path = job_dir / "progress.json"
    last_update = 0.0

    def progress(**state):
        write_json(progress_path, state)

    def on_download(state):
        nonlocal last_update
        if state.get("downloaded_bytes", 0) > request["max_download_bytes"]:
            raise JobError("size")
        total = state.get("total_bytes") or state.get("total_bytes_estimate") or 0
        if total and total > request["max_download_bytes"]:
            raise JobError("size")
        now = time.monotonic()
        if now - last_update < 1:
            return
        last_update = now
        if directory_size(download_dir) > request["max_download_bytes"]:
            raise JobError("size")
        percent = round(min(100, 100 * state.get("downloaded_bytes", 0) / total)) if total else None
        progress(stage="downloading", percent=percent)

    quality = request["quality"]
    # "?" also accepts formats whose dimensions are not reported by a site.
    height = f"[height<=?{quality}]" if quality != "best" else ""
    formats = (f"bv*{height}[vcodec^=avc1]+ba[ext=m4a]/"
               f"b{height}[ext=mp4]/bv*{height}+ba/b{height}")
    runtimes = {name: {"path": path} for name in ("deno", "node")
                if (path := shutil.which(name))}
    options = {
        "format": formats,
        "outtmpl": str(download_dir / "source.%(ext)s"),
        "noplaylist": True, "lazy_playlist": True, "playlistend": 1,
        "playlist_items": "1",
        "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "cachedir": False, "noprogress": True,
        "restrictfilenames": True, "windowsfilenames": True,
        "socket_timeout": 20, "retries": 2, "fragment_retries": 2,
        "extractor_retries": 1, "concurrent_fragment_downloads": 1,
        "skip_unavailable_fragments": False,
        "max_filesize": request["max_download_bytes"],
        "progress_hooks": [on_download],
        "merge_output_format": "mp4/mkv", "overwrites": True,
        "external_downloader": {"default": "native"},
        "hls_prefer_native": True, "js_runtimes": runtimes,
        "remote_components": [], "proxy": "", "usenetrc": False,
        "allow_unplayable_formats": False,
    }
    # Work on a per-job copy: yt-dlp can save refreshed cookies on close.
    # This avoids races when two concurrent jobs use the same owner cookie jar.
    if request.get("cookies_file"):
        cookies_copy = job_dir / "cookies.txt"
        shutil.copyfile(request["cookies_file"], cookies_copy)
        try:
            cookies_copy.chmod(0o600)
        except OSError:
            pass
        options["cookiefile"] = str(cookies_copy)
    progress(stage="extracting")
    with PublicYoutubeDL(options, request) as downloader:
        info = downloader.extract_info(url, download=True)
    if not info:
        raise JobError("unsupported")
    check_info(info, request)
    sources = [item for item in download_dir.iterdir()
               if item.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".ts", ".m4v")
               and not item.is_symlink() and item.stat().st_size > 0]
    if not sources:
        raise JobError("unsupported")
    # A successful merge removes separate source streams. The largest media
    # file is the final video if an extractor leaves an auxiliary file behind.
    source = max(sources, key=lambda item: item.stat().st_size)
    if source.stat().st_size > request["max_download_bytes"]:
        raise JobError("size")
    parts = prepare_video(source, job_dir / "output",
                          upload_limit=request["upload_limit"], max_parts=request["max_parts"],
                          max_duration=request["max_duration"], progress=progress)
    title = " ".join(str(info.get("title") or "Видео").split())[:180]
    return {"ok": True, "title": title, "parts": parts,
            "source": (info.get("extractor_key") or info.get("extractor") or "Сайт")[:60]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    job_dir = args.job.resolve()
    request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    os.environ["YTDLP_NO_PLUGINS"] = "1"
    FFmpegFD.real_download = block_external_download
    install_network_guard()
    try:
        result = download(request, job_dir)
    except UnsafeURL:
        result = {"ok": False, "error": "unsafe"}
    except JobError as exc:
        result = {"ok": False, "error": exc.code}
    except DownloadError as exc:
        result = {"ok": False, "error": classify_error(str(exc))}
    except OSError as exc:
        result = {"ok": False, "error": "storage" if exc.errno == 28 else "network"}
    except Exception:
        result = {"ok": False, "error": "internal"}
    write_json(job_dir / "result.json", result)
    # Never print raw URLs, cookies or source errors; parent only uses the JSON.
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
