from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from app.config import settings
from app.errors import JobNotFoundError


JobStatus = Literal["queued", "downloading", "loading_model", "processing", "encoding", "completed", "failed"]


@dataclass
class Job:
    id: str
    filename: str
    status: JobStatus
    progress: int
    message: str
    created_at: str
    input_path: str
    output_path: str
    error: str | None = None
    metadata: dict | None = None

    def public(self) -> dict:
        data = asdict(self)
        data.pop("input_path")
        data.pop("output_path")
        data["download_url"] = f"/api/jobs/{self.id}/download" if self.status == "completed" else None
        data["preview_url"] = f"/api/jobs/{self.id}/preview" if self.status == "completed" else None
        data["source_url"] = f"/api/jobs/{self.id}/source"
        return data


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()
        self._load_existing()

    def _metadata_path(self, job_id: str) -> Path:
        return settings.jobs_dir / job_id / "job.json"

    def _save(self, job: Job) -> None:
        path = self._metadata_path(job.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(job), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _load_existing(self) -> None:
        for path in settings.jobs_dir.glob("*/job.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**data)
                if job.status not in {"completed", "failed"}:
                    job.status = "failed"
                    job.message = "服务重启导致任务中断"
                    job.error = "任务未完成，请重新提交"
                    self._save(job)
                self._jobs[job.id] = job
            except (OSError, ValueError, TypeError):
                continue

    def create(self, filename: str, suffix: str) -> Job:
        job_id = uuid4().hex
        job_dir = settings.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        job = Job(
            id=job_id,
            filename=filename,
            status="queued",
            progress=0,
            message="等待处理",
            created_at=datetime.now(timezone.utc).isoformat(),
            input_path=str(job_dir / f"source{suffix}"),
            output_path=str(job_dir / "depth.mp4"),
        )
        with self._lock:
            self._jobs[job_id] = job
            self._save(job)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def update(self, job_id: str, **changes) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            for key, value in changes.items():
                setattr(job, key, value)
            self._save(job)
            return job

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)


job_store = JobStore()
