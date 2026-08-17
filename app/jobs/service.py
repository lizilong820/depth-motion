from __future__ import annotations

import logging
from pathlib import Path
import shutil
from threading import Lock

from app.config import settings
from app.depth.video import process_depth_video
from app.errors import InvalidUploadError
from app.jobs.downloader import download_video
from app.jobs.options import ProcessingOptions
from app.jobs.platform_downloader import detect_platform, download_platform_video
from app.jobs.store import job_store


logger = logging.getLogger("depth-studio.jobs")
_job_execution_lock = Lock()
_job_reserved = False
_job_reservation_lock = Lock()


def reserve_job_slot() -> bool:
    global _job_reserved
    with _job_reservation_lock:
        if _job_reserved:
            return False
        _job_reserved = True
        return True


def release_job_slot() -> None:
    global _job_reserved
    with _job_reservation_lock:
        _job_reserved = False


def run_depth_job(job_id: str) -> None:
    with _job_execution_lock:
        _run_depth_job(job_id)


def run_remote_depth_job(job_id: str, url: str) -> None:
    with _job_execution_lock:
        placeholder: Path | None = None
        try:
            job = job_store.get(job_id)
            placeholder = Path(job.input_path)
            platform = detect_platform(url)
            message = "正在解析视频页面" if platform else "正在下载远程视频"
            job_store.update(job_id, status="downloading", progress=1, message=message)
            if platform:
                result = download_platform_video(url, placeholder.parent, settings.max_upload_bytes)
                source_path = result.source_path
                filename = result.filename
                platform = result.platform
            else:
                result = download_video(url, placeholder.parent, settings.max_upload_bytes)
                source_path = placeholder.parent / f"source{result.suffix}"
                filename = result.filename
            job_store.update(
                job_id,
                filename=filename,
                input_path=str(source_path),
                source_platform=platform or "remote",
                message="视频下载完成，正在验证",
            )
            _run_depth_job(job_id, release_slot=False)
        except Exception as exc:
            logger.exception("remote_job_failed job_id=%s", job_id)
            public_error = exc.message if isinstance(exc, InvalidUploadError) else "视频下载或处理失败"
            try:
                job_store.update(
                    job_id,
                    status="failed",
                    message="远程视频处理失败",
                    error=public_error,
                )
            except Exception:
                logger.exception("remote_job_failure_status_update_failed job_id=%s", job_id)
        finally:
            try:
                if placeholder is not None and placeholder.exists():
                    current = job_store.get(job_id)
                    if Path(current.input_path) != placeholder:
                        placeholder.unlink(missing_ok=True)
            finally:
                release_job_slot()


def _run_depth_job(job_id: str, release_slot: bool = True) -> None:
    try:
        job = job_store.get(job_id)
        options = ProcessingOptions.from_dict(job.options)
        options.validate()
        job_store.update(job_id, status="loading_model", progress=3, message="正在验证视频并加载深度模型")
        last_reported = 0

        def report(frame: int, total: int) -> None:
            nonlocal last_reported
            interval = max(total // 100, 1)
            if frame != total and frame - last_reported < interval:
                return
            last_reported = frame
            percent = 5 + int((frame / max(total, 1)) * 88)
            finished_frames = frame >= total
            job_store.update(
                job_id,
                status="encoding" if finished_frames else "processing",
                progress=94 if finished_frames else min(percent, 93),
                message=(
                    "正在编码并打包 ComfyUI 素材"
                    if finished_frames and options.create_package
                    else "正在编码深度视频"
                    if finished_frames
                    else f"正在估计深度帧 {frame} / {total}"
                ),
            )

        metadata = process_depth_video(
            source_path=Path(job.input_path),
            output_path=Path(job.output_path),
            work_dir=Path(job.output_path).parent,
            options=options,
            progress=report,
            source_platform=job.source_platform,
        )
        job_store.update(
            job_id,
            status="completed",
            progress=100,
            message="ComfyUI 深度素材已生成" if options.create_package else "深度视频已生成",
            metadata=metadata,
        )
    except Exception as exc:
        logger.exception("depth_job_failed job_id=%s", job_id)
        public_error = exc.message if isinstance(exc, InvalidUploadError) else "深度视频处理失败"
        try:
            job = job_store.get(job_id)
            work_dir = Path(job.output_path).parent
            for filename in (
                "source-normalized.mp4", "depth-silent.mp4", "depth.mp4",
                "manifest.json", "depth-frames.zip", "depth-frames.zip.tmp",
                "comfyui-package.zip", "comfyui-package.zip.tmp",
            ):
                (work_dir / filename).unlink(missing_ok=True)
            shutil.rmtree(work_dir / "depth-frames", ignore_errors=True)
        except Exception:
            logger.exception("depth_job_cleanup_failed job_id=%s", job_id)
        job_store.update(
            job_id,
            status="failed",
            message="处理失败",
            error=public_error,
        )
    finally:
        if release_slot:
            release_job_slot()
