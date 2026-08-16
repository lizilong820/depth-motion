from __future__ import annotations

from collections.abc import Callable
import math
from pathlib import Path
import shutil
import subprocess

import cv2
import numpy as np


ProgressCallback = Callable[[int, int], None]


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = result.stderr.strip().splitlines()
        raise RuntimeError(details[-1] if details else "FFmpeg 处理失败")


def process_depth_video(
    source_path: Path,
    output_path: Path,
    work_dir: Path,
    invert: bool,
    temporal_smoothing: float,
    progress: ProgressCallback,
) -> dict[str, int | float | str]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("未找到 FFmpeg，请先安装并加入 PATH")

    capture = cv2.VideoCapture(str(source_path))
    if not capture.isOpened():
        raise RuntimeError("无法读取上传的视频")

    fps = float(capture.get(cv2.CAP_PROP_FPS))
    if not math.isfinite(fps) or fps <= 0 or fps > 240:
        fps = 24.0
    source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    max_side = max(source_width, source_height)
    scale = min(1.0, 1920 / max(max_side, 1))
    width = max(2, int(source_width * scale) // 2 * 2)
    height = max(2, int(source_height * scale) // 2 * 2)
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if width < 2 or height < 2 or total_frames < 1:
        capture.release()
        raise RuntimeError("视频信息无效")

    raw_video = work_dir / "depth-silent.mp4"
    writer = cv2.VideoWriter(
        str(raw_video),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
        True,
    )
    if not writer.isOpened():
        capture.release()
        raise RuntimeError("无法创建深度视频编码器")

    from app.depth.estimator import get_estimator

    estimator = get_estimator()
    frame_index = 0
    previous_depth: np.ndarray | None = None
    smoothing = min(max(temporal_smoothing, 0.0), 0.9)
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            depth = estimator.predict(frame, invert=invert)
            if previous_depth is not None and smoothing > 0:
                depth = cv2.addWeighted(depth, 1 - smoothing, previous_depth, smoothing, 0)
            previous_depth = depth
            writer.write(cv2.cvtColor(depth, cv2.COLOR_GRAY2BGR))
            frame_index += 1
            progress(frame_index, total_frames)
        if frame_index < 1:
            raise RuntimeError("视频中没有可处理的画面")
    finally:
        capture.release()
        writer.release()

    try:
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", str(raw_video), "-i", str(source_path),
            "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-preset", "medium",
            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            "-movflags", "+faststart", str(output_path),
        ])
    finally:
        raw_video.unlink(missing_ok=True)

    return {
        "frames": frame_index,
        "fps": round(fps, 3),
        "width": width,
        "height": height,
        "device": str(estimator.device),
    }
