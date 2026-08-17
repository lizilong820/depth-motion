from __future__ import annotations

from pathlib import Path
import shutil

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from app.config import settings
from app.depth.probe import probe_video
from app.errors import InvalidUploadError, JobNotReadyError, ServerBusyError
from app.jobs.downloader import validate_remote_target
from app.jobs.options import FitMode, PresetName, ProcessingOptions
from app.jobs.platform_downloader import detect_platform, validate_platform_url
from app.jobs.service import release_job_slot, reserve_job_slot, run_depth_job, run_remote_depth_job
from app.jobs.store import Job, job_store


router = APIRouter(prefix="/api/jobs", tags=["jobs"])
ALLOWED_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


class ProcessingRequest(BaseModel):
    preset: PresetName = "standard_depth"
    invert: bool = False
    temporal_smoothing: float = Field(default=0.25, ge=0, le=0.9)
    start_time: float = Field(default=0, ge=0)
    end_time: float | None = Field(default=None, gt=0)
    output_width: int | None = Field(default=None, ge=128, le=1920)
    output_height: int | None = Field(default=None, ge=128, le=1920)
    output_fps: float | None = Field(default=None, ge=1, le=60)
    max_output_side: int | None = Field(default=None, ge=128, le=1920)
    max_output_fps: float | None = Field(default=None, ge=1, le=60)
    fit_mode: FitMode = "contain"
    export_png: bool = False
    create_package: bool = False
    stabilize_range: float = Field(default=0.85, ge=0, le=0.98)
    scene_cut_reset: bool = True
    scene_cut_threshold: float = Field(default=0.32, ge=0.05, le=1)

    @model_validator(mode="after")
    def validate_times(self):
        if self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("结束时间必须晚于开始时间")
        return self

    def options(self) -> ProcessingOptions:
        value = ProcessingOptions(**self.model_dump())
        value.validate()
        return value


class RemoteJobRequest(ProcessingRequest):
    url: str = Field(min_length=8, max_length=2048)

    def options(self) -> ProcessingOptions:
        return ProcessingOptions(**self.model_dump(exclude={"url"}))


def _form_options(
    preset: PresetName,
    invert: bool,
    temporal_smoothing: float,
    start_time: float,
    end_time: float | None,
    output_width: int | None,
    output_height: int | None,
    output_fps: float | None,
    max_output_side: int | None,
    max_output_fps: float | None,
    fit_mode: FitMode,
    export_png: bool,
    create_package: bool,
    stabilize_range: float,
    scene_cut_reset: bool,
    scene_cut_threshold: float,
) -> ProcessingOptions:
    options = ProcessingOptions(
        preset=preset,
        invert=invert,
        temporal_smoothing=temporal_smoothing,
        start_time=start_time,
        end_time=end_time,
        output_width=output_width,
        output_height=output_height,
        output_fps=output_fps,
        max_output_side=max_output_side,
        max_output_fps=max_output_fps,
        fit_mode=fit_mode,
        export_png=export_png,
        create_package=create_package,
        stabilize_range=stabilize_range,
        scene_cut_reset=scene_cut_reset,
        scene_cut_threshold=scene_cut_threshold,
    )
    options.validate()
    return options


@router.post("", status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    preset: PresetName = Form("standard_depth"),
    invert: bool = Form(False),
    temporal_smoothing: float = Form(0.25, ge=0, le=0.9),
    start_time: float = Form(0, ge=0),
    end_time: float | None = Form(None, gt=0),
    output_width: int | None = Form(None, ge=128, le=1920),
    output_height: int | None = Form(None, ge=128, le=1920),
    output_fps: float | None = Form(None, ge=1, le=60),
    max_output_side: int | None = Form(None, ge=128, le=1920),
    max_output_fps: float | None = Form(None, ge=1, le=60),
    fit_mode: FitMode = Form("contain"),
    export_png: bool = Form(False),
    create_package: bool = Form(False),
    stabilize_range: float = Form(0.85, ge=0, le=0.98),
    scene_cut_reset: bool = Form(True),
    scene_cut_threshold: float = Form(0.32, ge=0.05, le=1),
) -> dict:
    options = _form_options(
        preset, invert, temporal_smoothing, start_time, end_time, output_width,
        output_height, output_fps, max_output_side, max_output_fps,
        fit_mode, export_png, create_package,
        stabilize_range, scene_cut_reset, scene_cut_threshold,
    )
    filename = Path(file.filename or "video.mp4").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES or not (file.content_type or "").startswith("video/"):
        raise InvalidUploadError("仅支持常见视频文件：MP4、MOV、WebM、MKV、AVI、M4V")
    if not reserve_job_slot():
        raise ServerBusyError()

    job = None
    size = 0
    try:
        job = job_store.create(
            filename=filename,
            suffix=suffix,
            options=options.public(),
            source_platform="upload",
        )
        with Path(job.input_path).open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise InvalidUploadError(f"视频不能超过 {settings.max_upload_mb} MB")
                destination.write(chunk)
        probe_video(Path(job.input_path))
    except Exception:
        release_job_slot()
        if job is not None:
            job_store.delete(job.id)
            shutil.rmtree(Path(job.input_path).parent, ignore_errors=True)
        raise
    finally:
        await file.close()

    background_tasks.add_task(run_depth_job, job.id)
    return job.public()


@router.post("/remote", status_code=202)
def create_remote_job(request: RemoteJobRequest, background_tasks: BackgroundTasks) -> dict:
    options = request.options()
    options.validate()
    if not reserve_job_slot():
        raise ServerBusyError()

    job = None
    try:
        platform = detect_platform(request.url)
        if platform:
            validate_platform_url(request.url)
        else:
            validate_remote_target(request.url)
        job = job_store.create(
            filename="远程视频",
            suffix=".download",
            options=options.public(),
            source_platform=platform or "remote",
        )
        background_tasks.add_task(run_remote_depth_job, job.id, request.url)
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
    _require_completed(job)
    return FileResponse(job.output_path, media_type="video/mp4")


@router.get("/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    job = job_store.get(job_id)
    _require_completed(job)
    stem = Path(job.filename).stem
    return FileResponse(job.output_path, media_type="video/mp4", filename=f"{stem}-depth.mp4")


@router.get("/{job_id}/manifest")
def manifest_job(job_id: str) -> FileResponse:
    return _artifact_response(job_id, "manifest", "application/json", "manifest.json")


@router.get("/{job_id}/frames")
def frames_job(job_id: str) -> FileResponse:
    return _artifact_response(job_id, "frames", "application/zip", "depth-frames.zip")


@router.get("/{job_id}/package")
def package_job(job_id: str) -> FileResponse:
    return _artifact_response(job_id, "package", "application/zip", "comfyui-package.zip")


def _require_completed(job: Job) -> None:
    if job.status != "completed":
        raise JobNotReadyError()


def _artifact_response(job_id: str, kind: str, media_type: str, filename: str) -> FileResponse:
    job = job_store.get(job_id)
    _require_completed(job)
    path = job.artifact_path(kind)
    if path is None or not path.is_file():
        raise JobNotReadyError()
    return FileResponse(path, media_type=media_type, filename=filename)
