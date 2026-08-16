from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


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
