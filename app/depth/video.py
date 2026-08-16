from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import cv2
import numpy as np

from app.depth.probe import probe_video, resolve_output
from app.jobs.options import ProcessingOptions


ProgressCallback = Callable[[int, int], None]


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = result.stderr.strip().splitlines()
        raise RuntimeError(details[-1] if details else "FFmpeg 处理失败")


def _video_filter(width: int, height: int, fps: float, fit_mode: str) -> str:
    if fit_mode == "stretch":
        resize = f"scale={width}:{height}"
    elif fit_mode == "cover":
        resize = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    else:
        resize = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    return f"{resize},fps={fps:.6f},setsar=1,format=yuv420p"


def _prepare_source(source_path: Path, normalized_path: Path, output: dict) -> None:
    _run_ffmpeg([
        "ffmpeg", "-y", "-ss", str(output["start_time"]), "-i", str(source_path),
        "-t", str(output["duration"]), "-vf",
        _video_filter(output["width"], output["height"], output["fps"], output["fit_mode"]),
        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "15", "-c:a", "aac", "-movflags", "+faststart", str(normalized_path),
    ])


def _scene_difference(previous: np.ndarray | None, frame: np.ndarray) -> tuple[float, np.ndarray]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sample = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
    if previous is None:
        return 0.0, sample
    difference = float(np.mean(cv2.absdiff(previous, sample))) / 255.0
    return difference, sample


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path, kind: str) -> dict:
    return {
        "type": kind,
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _write_zip(zip_path: Path, entries: list[tuple[Path, str]]) -> None:
    temporary = zip_path.with_suffix(zip_path.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, name in entries:
            archive.write(source, name)
    temporary.replace(zip_path)


def process_depth_video(
    source_path: Path,
    output_path: Path,
    work_dir: Path,
    options: ProcessingOptions,
    progress: ProgressCallback,
    source_platform: str | None = None,
) -> dict:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("未找到 FFmpeg，请先安装并加入 PATH")

    probe = probe_video(source_path)
    output = resolve_output(probe, options)
    normalized_source = work_dir / "source-normalized.mp4"
    raw_video = work_dir / "depth-silent.mp4"
    frames_dir = work_dir / "depth-frames"
    manifest_path = work_dir / "manifest.json"
    frames_zip_path = work_dir / "depth-frames.zip"
    package_path = work_dir / "comfyui-package.zip"
    need_frames = options.export_png or options.create_package

    _prepare_source(source_path, normalized_source, output)
    capture = cv2.VideoCapture(str(normalized_source))
    if not capture.isOpened():
        raise RuntimeError("无法读取标准化视频")
    writer = cv2.VideoWriter(
        str(raw_video),
        cv2.VideoWriter_fourcc(*"mp4v"),
        output["fps"],
        (output["width"], output["height"]),
        True,
    )
    if not writer.isOpened():
        capture.release()
        raise RuntimeError("无法创建深度视频编码器")
    if need_frames:
        frames_dir.mkdir(exist_ok=True)

    from app.depth.estimator import get_estimator

    estimator = get_estimator()
    expected_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or output["frames"]
    frame_index = 0
    previous_depth: np.ndarray | None = None
    previous_scene: np.ndarray | None = None
    stable_low: float | None = None
    stable_high: float | None = None
    scene_cuts: list[int] = []
    smoothing = min(max(options.temporal_smoothing, 0.0), 0.9)
    range_weight = min(max(options.stabilize_range, 0.0), 0.98)

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            difference, scene_sample = _scene_difference(previous_scene, frame)
            scene_cut = (
                options.scene_cut_reset
                and previous_scene is not None
                and difference >= options.scene_cut_threshold
            )
            previous_scene = scene_sample
            if scene_cut:
                previous_depth = None
                stable_low = None
                stable_high = None
                scene_cuts.append(frame_index + 1)

            raw_depth = estimator.predict_raw(frame)
            current_low, current_high = np.percentile(raw_depth, (2, 98))
            if stable_low is None or stable_high is None:
                stable_low, stable_high = float(current_low), float(current_high)
            else:
                stable_low = range_weight * stable_low + (1 - range_weight) * float(current_low)
                stable_high = range_weight * stable_high + (1 - range_weight) * float(current_high)
            normalized = np.clip(
                (raw_depth - stable_low) / max(stable_high - stable_low, 1e-6), 0, 1
            )
            if options.invert:
                normalized = 1 - normalized
            depth = (normalized * 255).astype(np.uint8)
            if previous_depth is not None and smoothing > 0:
                depth = cv2.addWeighted(depth, 1 - smoothing, previous_depth, smoothing, 0)
            previous_depth = depth

            writer.write(cv2.cvtColor(depth, cv2.COLOR_GRAY2BGR))
            frame_index += 1
            if need_frames:
                if not cv2.imwrite(str(frames_dir / f"{frame_index:06d}.png"), depth):
                    raise RuntimeError("无法写入深度 PNG 序列")
            progress(frame_index, expected_frames)
        if frame_index < 1:
            raise RuntimeError("视频中没有可处理的画面")
    finally:
        capture.release()
        writer.release()

    try:
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", str(raw_video),
            "-ss", str(output["start_time"]), "-i", str(source_path),
            "-t", str(output["duration"]), "-map", "0:v:0", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            "-movflags", "+faststart", str(output_path),
        ])
    finally:
        raw_video.unlink(missing_ok=True)

    output["frames"] = frame_index
    output["duration"] = round(frame_index / output["fps"], 3)
    manifest = {
        "schema_version": 1,
        "source": {
            "filename": source_path.name,
            "platform": source_platform or "upload",
            **probe.public(),
        },
        "output": output,
        "depth": {
            "convention": "black_near" if options.invert else "white_near",
            "temporal_smoothing": smoothing,
            "range_stabilization": range_weight,
            "normalization": "exponential_percentile_2_98",
            "scene_cut_reset": options.scene_cut_reset,
            "scene_cut_threshold": options.scene_cut_threshold,
            "scene_cuts": scene_cuts,
        },
        "artifacts": [],
    }
    manifest["artifacts"].append(_artifact(output_path, "depth_mp4"))
    frame_files = sorted(frames_dir.glob("*.png")) if need_frames else []
    if options.create_package:
        manifest["package_contents"] = ["depth.mp4", "source.mp4", "manifest.json"]
        manifest["package_contents"].extend(
            f"depth-frames/{path.name}" for path in frame_files
        )

    if options.export_png:
        _write_zip(frames_zip_path, [(path, f"depth-frames/{path.name}") for path in frame_files])
        manifest["artifacts"].append(_artifact(frames_zip_path, "depth_frames_zip"))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    package_artifact = None
    if options.create_package:
        entries = [
            (output_path, "depth.mp4"),
            (normalized_source, "source.mp4"),
            (manifest_path, "manifest.json"),
        ]
        entries.extend((path, f"depth-frames/{path.name}") for path in frame_files)
        _write_zip(package_path, entries)
        package_artifact = _artifact(package_path, "comfyui_package")

    normalized_source.unlink(missing_ok=True)
    if need_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return {
        "frames": frame_index,
        "fps": output["fps"],
        "width": output["width"],
        "height": output["height"],
        "duration": output["duration"],
        "device": str(estimator.device),
        "scene_cuts": len(scene_cuts),
        "artifacts": manifest["artifacts"] + ([package_artifact] if package_artifact else []),
    }
