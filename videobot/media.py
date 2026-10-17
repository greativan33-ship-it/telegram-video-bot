"""All FFmpeg commands operate on local files, with network protocols disabled."""

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import JobError


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None


def run_tool(arguments: list[str], timeout: int = 300) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                                check=False, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise JobError("timeout") from exc
    if result.returncode:
        raise JobError("processing")
    return result.stdout


def probe(path: Path) -> MediaInfo:
    result = json.loads(run_tool([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe,crypto,data",
        "-show_format", "-show_streams", "-of", "json", str(path),
    ], timeout=30))
    video = next((item for item in result.get("streams", [])
                  if item.get("codec_type") == "video"
                  and not item.get("disposition", {}).get("attached_pic")), None)
    audio = next((item for item in result.get("streams", [])
                  if item.get("codec_type") == "audio"), None)
    if not video:
        raise JobError("unsupported")
    try:
        duration = float(result.get("format", {}).get("duration") or video.get("duration") or 0)
    except (ValueError, TypeError) as exc:
        raise JobError("processing") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise JobError("processing")
    return MediaInfo(duration, int(video.get("width") or 0), int(video.get("height") or 0),
                     video.get("codec_name", ""), audio.get("codec_name") if audio else None)


def ffmpeg_input(path: Path) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "2", "-protocol_whitelist", "file,pipe,crypto,data", "-i", str(path)]


def codecs(info: MediaInfo) -> list[str]:
    result = ["-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1"]
    if info.video_codec == "h264":
        result += ["-c:v", "copy"]
    else:
        result += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                   "-threads", "2", "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    result += ["-c:a", "copy"] if info.audio_codec in (None, "aac") else [
        "-c:a", "aac", "-b:a", "128k"]
    return result


def prepare_video(source: Path, output_dir: Path, *, upload_limit: int,
                  max_parts: int, max_duration: int, progress=lambda **kw: None) -> list[dict]:
    info = probe(source)
    if info.duration > max_duration + 1:
        raise JobError("duration")
    output_dir.mkdir(parents=True, exist_ok=True)
    progress(stage="processing")
    normalized = output_dir / "video.mp4"
    if (source.suffix.lower() == ".mp4" and info.video_codec == "h264"
            and info.audio_codec in (None, "aac") and source.stat().st_size <= upload_limit):
        return [describe(source)]

    run_tool(ffmpeg_input(source) + codecs(info) + ["-movflags", "+faststart", str(normalized)],
             timeout=900)
    if normalized.stat().st_size <= upload_limit:
        return [describe(normalized)]

    # Segment-copy preserves quality and cuts on keyframes. A reserve absorbs
    # variable bitrate; unusually large segments are re-encoded below.
    target = int(upload_limit * 0.72)
    estimate = math.ceil(normalized.stat().st_size / target)
    if estimate > max_parts:
        raise JobError("parts")
    segment_seconds = max(0.25, info.duration / estimate)
    parts_dir = output_dir / "parts"
    parts_dir.mkdir()
    progress(stage="splitting")
    run_tool(ffmpeg_input(normalized) + [
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
        "-segment_time", f"{segment_seconds:.6f}", "-reset_timestamps", "1",
        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
        str(parts_dir / "part-%04d.mp4"),
    ], timeout=900)
    parts = sorted(parts_dir.glob("part-*.mp4"))
    if not parts:
        raise JobError("processing")
    final: list[Path] = []
    for part in parts:
        if part.stat().st_size <= upload_limit:
            final.append(part)
        else:
            # Long GOP or a bitrate spike: encode timed sections so every part
            # remains playable and fits the Telegram limit.
            part_info = probe(part)
            count = max(2, math.ceil(part.stat().st_size / target))
            if len(final) + count > max_parts:
                raise JobError("parts")
            span = part_info.duration / count
            video_rate = max(64_000, int(upload_limit * 0.78 * 8 / span) - 96_000)
            for index in range(count):
                path = parts_dir / f"{part.stem}-fixed-{index:03d}.mp4"
                run_tool(ffmpeg_input(part) + [
                    "-ss", f"{index * span:.6f}", "-t", f"{span:.6f}",
                    "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                    "-preset", "veryfast", "-threads", "2", "-pix_fmt", "yuv420p",
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    "-b:v", str(video_rate), "-maxrate", str(video_rate),
                    "-bufsize", str(video_rate * 2), "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart", str(path),
                ], timeout=600)
                if path.stat().st_size > upload_limit:
                    raise JobError("parts")
                final.append(path)
        if len(final) > max_parts:
            raise JobError("parts")
    return [describe(path) for path in final]


def describe(path: Path) -> dict:
    info = probe(path)
    return {"path": str(path.resolve()), "duration": math.ceil(info.duration),
            "width": info.width, "height": info.height, "size": path.stat().st_size}
