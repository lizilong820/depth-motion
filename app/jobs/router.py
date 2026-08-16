from __future__ import annotations

from pathlib import Path
import shutil

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.errors import InvalidUploadError, JobNotReadyError, ServerBusyError
from app.jobs.downloader import validate_remote_target
from app.jobs.platform_downloader import detect_platform, validate_platform_url
from app.jobs.service import (
    release_job_slot,
    reserve_job_slot,
    run_depth_job,
    run_remote_depth_job,
)
from app.jobs.store import job_store


router = APIRouter(prefix="/api/jobs", tags=["jobs"])
ALLOWED_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


class RemoteJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    invert: bool = False
    temporal_smoothing: float = Field(default=0.25, ge=0, le=0.9)


@router.post("", status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    invert: bool = Form(False),
    temporal_smoothing: float = Form(0.25, ge=0, le=0.9),
) -> dict:
    filename = Path(file.filename or "video.mp4").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES or not (file.content_type or "").startswith("video/"):
        raise InvalidUploadError("仅支持常见视频文件：MP4、MOV、WebM、MKV、AVI、M4V")

    if not reserve_job_slot():
        raise ServerBusyError()

    job = None
    size = 0
    try:
        job = job_store.create(filename=filename, suffix=suffix)
        with Path(job.input_path).open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise InvalidUploadError(f"视频不能超过 {settings.max_upload_mb} MB")
                destination.write(chunk)
    except Exception:
        release_job_slot()
        if job is not None:
            job_store.delete(job.id)
            shutil.rmtree(Path(job.input_path).parent, ignore_errors=True)
        raise
    finally:
        await file.close()

    background_tasks.add_task(run_depth_job, job.id, invert, temporal_smoothing)
    return job.public()


@router.post("/remote", status_code=202)
def create_remote_job(request: RemoteJobRequest, background_tasks: BackgroundTasks) -> dict:
    if not reserve_job_slot():
        raise ServerBusyError()

    job = None
    try:
        if detect_platform(request.url):
            validate_platform_url(request.url)
        else:
            validate_remote_target(request.url)
        job = job_store.create(filename="远程视频", suffix=".download")
        background_tasks.add_task(
            run_remote_depth_job,
            job.id,
            request.url,
            request.invert,
            request.temporal_smoothing,
        )
        return job.public()
    except Exception:
        release_job_slot()
        if job is not None:
            job_store.delete(job.id)
            shutil.rmtree(Path(job.input_path).parent, ignore_errors=True)
        raise


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    return job_store.get(job_id).public()


@router.get("/{job_id}/source")
def source_job(job_id: str) -> FileResponse:
    job = job_store.get(job_id)
    return FileResponse(job.input_path)


@router.get("/{job_id}/preview")
def preview_job(job_id: str) -> FileResponse:
    job = job_store.get(job_id)
    if job.status != "completed":
        raise JobNotReadyError()
    return FileResponse(job.output_path, media_type="video/mp4")


@router.get("/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    job = job_store.get(job_id)
    if job.status != "completed":
        raise JobNotReadyError()
    stem = Path(job.filename).stem
    return FileResponse(
        job.output_path,
        media_type="video/mp4",
        filename=f"{stem}-depth.mp4",
    )
