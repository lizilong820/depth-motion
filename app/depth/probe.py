from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess

from app.config import settings
from app.errors import InvalidUploadError
from app.jobs.options import ProcessingOptions


@dataclass(frozen=True)
class VideoProbe:
    width: int
    height: int
    fps: float
    frames: int
    duration: float
    codec: str
    has_audio: bool

    def public(self) -> dict:
        return asdict(self)


def _fraction(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / max(float(denominator), 1e-9)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path: Path) -> VideoProbe:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("未找到 ffprobe，请先安装 FFmpeg")
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise InvalidUploadError("视频信息读取超时") from exc
    if result.returncode != 0:
        raise InvalidUploadError("文件不是可解码的视频")
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        video = next(item for item in streams if item.get("codec_type") == "video")
    except (json.JSONDecodeError, StopIteration, TypeError) as exc:
        raise InvalidUploadError("文件中没有有效的视频流") from exc

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate"))
    duration = float(video.get("duration") or payload.get("format", {}).get("duration") or 0)
    frames = int(video.get("nb_frames") or 0)
    if frames <= 0 and fps > 0 and duration > 0:
        frames = int(math.ceil(fps * duration))
    if width < 2 or height < 2 or fps <= 0 or duration <= 0 or frames < 1:
        raise InvalidUploadError("视频尺寸、帧率或时长信息无效")

    probe = VideoProbe(
        width=width,
        height=height,
        fps=fps,
        frames=frames,
        duration=duration,
        codec=str(video.get("codec_name") or "unknown"),
        has_audio=any(item.get("codec_type") == "audio" for item in streams),
    )
    validate_video_limits(probe)
    return probe


def validate_video_limits(probe: VideoProbe) -> None:
    if probe.duration > settings.max_video_seconds:
        raise InvalidUploadError(f"视频不能超过 {settings.max_video_seconds} 秒")
    if probe.width * probe.height > settings.max_video_pixels:
        raise InvalidUploadError("视频分辨率超过处理上限")
    if probe.frames > settings.max_video_frames:
        raise InvalidUploadError(f"视频不能超过 {settings.max_video_frames} 帧")


def resolve_output(probe: VideoProbe, options: ProcessingOptions) -> dict:
    options.validate()
    end = min(options.end_time if options.end_time is not None else probe.duration, probe.duration)
    if options.start_time >= probe.duration or end <= options.start_time:
        raise InvalidUploadError("截取时间超出视频范围")

    source_ratio = probe.width / probe.height
    width = options.output_width
    height = options.output_height
    if width is None and height is None:
        scale = min(1.0, 1920 / max(probe.width, probe.height))
        width = int(probe.width * scale)
        height = int(probe.height * scale)
    elif width is None:
        width = int(height * source_ratio)
        if width > 1920:
            height = int(height * 1920 / width)
            width = 1920
    elif height is None:
        height = int(width / source_ratio)
        if height > 1920:
            width = int(width * 1920 / height)
            height = 1920
    width = max(2, int(width) // 2 * 2)
    height = max(2, int(height) // 2 * 2)
    fps = min(options.output_fps or probe.fps, probe.fps, 60.0)
    duration = end - options.start_time
    frames = max(1, int(round(duration * fps)))
    if frames > settings.max_video_frames:
        raise InvalidUploadError(f"输出不能超过 {settings.max_video_frames} 帧")
    return {
        "start_time": round(options.start_time, 3),
        "end_time": round(end, 3),
        "duration": round(duration, 3),
        "width": width,
        "height": height,
        "fps": round(fps, 6),
        "frames": frames,
        "fit_mode": options.fit_mode,
    }
