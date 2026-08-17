from io import BytesIO
from pathlib import Path
import shutil
import zipfile

from fastapi.testclient import TestClient

from app.jobs.store import job_store
from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_downloads_installable_comfyui_node_package() -> None:
    response = client.get("/api/comfyui-node")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert "comfyui_depth_motion.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert archive.namelist() == [
            "comfyui_depth_motion/__init__.py",
            "comfyui_depth_motion/client.py",
            "comfyui_depth_motion/nodes.py",
            "comfyui_depth_motion/requirements.txt",
            "comfyui_depth_motion/README.md",
        ]
        assert b"NODE_CLASS_MAPPINGS" in archive.read("comfyui_depth_motion/__init__.py")


def test_rejects_non_video_upload() -> None:
    response = client.post(
        "/api/jobs",
        files={"file": ("notes.txt", b"not a video", "text/plain")},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_UPLOAD"


def test_missing_job() -> None:
    response = client.get("/api/jobs/missing")
    assert response.status_code == 404
    assert response.json()["code"] == "JOB_NOT_FOUND"


def test_remote_job_rejects_invalid_time_range() -> None:
    response = client.post(
        "/api/jobs/remote",
        json={"url": "https://example.com/video.mp4", "start_time": 4, "end_time": 2},
    )
    assert response.status_code == 422


def test_remote_job_rejects_invalid_preset_parameters() -> None:
    invalid_payloads = [
        {"preset": "unknown"},
        {"max_output_side": 127},
        {"max_output_fps": 61},
    ]
    for payload in invalid_payloads:
        response = client.post(
            "/api/jobs/remote",
            json={"url": "https://example.com/video.mp4", **payload},
        )
        assert response.status_code == 422


def test_upload_job_rejects_invalid_preset_parameters() -> None:
    response = client.post(
        "/api/jobs",
        data={"preset": "quick_preview", "max_output_side": "127"},
        files={"file": ("clip.mp4", b"not reached", "video/mp4")},
    )
    assert response.status_code == 422


def test_completed_job_exposes_and_downloads_artifacts() -> None:
    job = job_store.create(filename="clip.mp4", suffix=".mp4")
    try:
        directory = job.directory
        Path(job.output_path).write_bytes(b"depth-video")
        (directory / "manifest.json").write_text('{"frames": 2}', encoding="utf-8")
        (directory / "depth-frames.zip").write_bytes(b"frames-zip")
        (directory / "comfyui-package.zip").write_bytes(b"package-zip")
        job_store.update(job.id, status="completed", progress=100)

        details = client.get(f"/api/jobs/{job.id}").json()
        assert details["manifest_url"] == f"/api/jobs/{job.id}/manifest"
        assert details["frames_url"] == f"/api/jobs/{job.id}/frames"
        assert details["package_url"] == f"/api/jobs/{job.id}/package"

        expected = {
            "manifest": ("application/json", b'{"frames": 2}'),
            "frames": ("application/zip", b"frames-zip"),
            "package": ("application/zip", b"package-zip"),
        }
        for kind, (media_type, content) in expected.items():
            response = client.get(f"/api/jobs/{job.id}/{kind}")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(media_type)
            assert response.content == content
    finally:
        job_store.delete(job.id)
        shutil.rmtree(job.directory, ignore_errors=True)


def test_artifacts_require_completed_job_and_existing_file() -> None:
    job = job_store.create(filename="clip.mp4", suffix=".mp4")
    try:
        response = client.get(f"/api/jobs/{job.id}/manifest")
        assert response.status_code == 409
        assert response.json()["code"] == "JOB_NOT_READY"

        job_store.update(job.id, status="completed", progress=100)
        response = client.get(f"/api/jobs/{job.id}/package")
        assert response.status_code == 409
        assert response.json()["code"] == "JOB_NOT_READY"
    finally:
        job_store.delete(job.id)
        shutil.rmtree(job.directory, ignore_errors=True)
