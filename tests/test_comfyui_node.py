from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import zipfile

import numpy as np
from PIL import Image
import pytest
import requests

from comfyui_depth_motion.client import (
    DepthMotionClient,
    DepthMotionError,
    preset_options,
)
from comfyui_depth_motion.nodes import (
    DepthMotionGenerate,
    DepthMotionLoadFrames,
    NODE_CLASS_MAPPINGS,
)


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b"", status_code: int = 200, headers=None) -> None:
        self.payload = payload
        self.content = content
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = headers or {}

    def json(self):
        if self.payload is None:
            raise json.JSONDecodeError("missing", "", 0)
        return self.payload

    def iter_content(self, chunk_size: int):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def png_bytes(value: int, size: tuple[int, int] = (4, 3)) -> bytes:
    buffer = BytesIO()
    Image.fromarray(np.full((size[1], size[0]), value, dtype=np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def write_frames_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)


def test_v1_node_registration_and_schema() -> None:
    assert set(NODE_CLASS_MAPPINGS) == {"DepthMotionGenerate", "DepthMotionLoadFrames"}
    assert DepthMotionGenerate.FUNCTION == "generate"
    assert DepthMotionLoadFrames.RETURN_TYPES == ("IMAGE", "INT")
    inputs = DepthMotionGenerate.INPUT_TYPES()["required"]
    assert inputs["service_url"][1]["default"] == "https://depth.whaios.com"
    assert "comfyui_package" in inputs["preset"][0]


def test_file_cache_keys_change_with_source_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"one")
    first = DepthMotionGenerate.IS_CHANGED(str(source), refresh=0)
    source.write_bytes(b"different")
    second = DepthMotionGenerate.IS_CHANGED(str(source), refresh=0)
    assert first != second
    assert DepthMotionGenerate.IS_CHANGED("https://video.example/1", refresh=0) != DepthMotionGenerate.IS_CHANGED(
        "https://video.example/1", refresh=1
    )

    archive = tmp_path / "frames.zip"
    archive.write_bytes(b"zip-one")
    frames_first = DepthMotionLoadFrames.IS_CHANGED(str(archive))
    archive.write_bytes(b"zip-two-different")
    assert frames_first != DepthMotionLoadFrames.IS_CHANGED(str(archive))


def test_preset_options_match_server_workflows() -> None:
    quick = preset_options("quick_preview", False)
    package = preset_options("comfyui_package", True)
    assert quick["max_output_side"] == 768
    assert quick["max_output_fps"] == 12
    assert package["create_package"] is True
    assert package["export_png"] is True
    assert package["invert"] is True
    with pytest.raises(DepthMotionError):
        preset_options("unknown", False)


def test_client_upload_wait_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    calls = []
    queued = {"id": "job-1", "status": "queued"}
    completed = {
        "id": "job-1",
        "status": "completed",
        "download_url": "/api/jobs/job-1/download",
        "manifest_url": "/api/jobs/job-1/manifest",
        "frames_url": None,
        "package_url": None,
    }

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return FakeResponse(queued)
        if url.endswith("/api/jobs/job-1"):
            return FakeResponse(completed)
        if url.endswith("/download"):
            return FakeResponse(content=b"depth")
        if url.endswith("/manifest"):
            return FakeResponse(content=b"{}")
        raise AssertionError(url)

    client = DepthMotionClient("https://depth.example", timeout=30, poll_interval=0.2)
    monkeypatch.setattr(client.session, "request", request)
    job = client.create_job(str(video), preset_options("quick_preview", False))
    job = client.wait(job["id"])
    artifacts = client.download_artifacts(job, tmp_path / "output")

    assert Path(artifacts["download_url"]).read_bytes() == b"depth"
    assert Path(artifacts["manifest_url"]).read_bytes() == b"{}"
    assert artifacts["frames_url"] == ""
    upload_call = calls[0]
    assert upload_call[0] == "POST"
    assert upload_call[2]["data"]["preset"] == "quick_preview"
    assert upload_call[2]["files"]["file"][0] == "source.mp4"


def test_client_validates_service_url_and_cleans_partial_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for invalid in (
        "ftp://depth.example",
        "https://user@depth.example",
        "https://depth.example/api",
        "https://depth.example?token=secret",
    ):
        with pytest.raises(DepthMotionError, match="服务地址"):
            DepthMotionClient(invalid)

    client = DepthMotionClient("https://depth.example")

    def broken_request(method, url, **kwargs):
        if method == "GET":
            class BrokenResponse(FakeResponse):
                def iter_content(self, chunk_size: int):
                    yield b"partial"
                    raise requests.ConnectionError("expected")
            return BrokenResponse()
        raise AssertionError(method)

    monkeypatch.setattr(client.session, "request", broken_request)
    with pytest.raises(DepthMotionError, match="下载 Depth Motion 产物失败"):
        client.download_artifacts(
            {
                "id": "broken",
                "download_url": "/api/jobs/broken/download",
                "manifest_url": None,
                "frames_url": None,
                "package_url": None,
            },
            tmp_path,
        )
    assert not list(tmp_path.rglob("*.part"))


def test_client_rejects_redirects_and_oversized_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = DepthMotionClient("https://depth.example")
    job = {
        "id": "unsafe",
        "download_url": "/api/jobs/unsafe/download",
        "manifest_url": None,
        "frames_url": None,
        "package_url": None,
    }
    monkeypatch.setattr(
        client.session,
        "request",
        lambda *args, **kwargs: FakeResponse(status_code=302, headers={"location": "http://127.0.0.1/"}),
    )
    with pytest.raises(DepthMotionError, match="重定向"):
        client.download_artifacts(job, tmp_path)

    monkeypatch.setattr(
        client.session,
        "request",
        lambda *args, **kwargs: FakeResponse(headers={"content-length": str(9 * 1024 * 1024 * 1024)}),
    )
    with pytest.raises(DepthMotionError, match="下载大小上限"):
        client.download_artifacts(job, tmp_path)
    assert not list(tmp_path.rglob("*.part"))


def test_client_remote_failure_and_cross_origin_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = DepthMotionClient("https://depth.example", timeout=30, poll_interval=0.2)
    responses = iter([
        FakeResponse({"id": "remote", "status": "queued"}),
        FakeResponse({"id": "remote", "status": "failed", "error": "expected"}),
    ])
    monkeypatch.setattr(client.session, "request", lambda *args, **kwargs: next(responses))
    job = client.create_job("https://video.example/clip.mp4", preset_options("standard_depth", False))
    with pytest.raises(DepthMotionError, match="expected"):
        client.wait(job["id"])

    with pytest.raises(DepthMotionError, match="非同源"):
        client.download_artifacts(
            {
                "id": "remote",
                "download_url": "https://attacker.example/depth.mp4",
                "manifest_url": None,
                "frames_url": None,
                "package_url": None,
            },
            tmp_path,
        )


def test_load_frames_returns_comfyui_image_batch(tmp_path: Path) -> None:
    archive = tmp_path / "frames.zip"
    write_frames_zip(
        archive,
        [(f"depth-frames/{index:06d}.png", png_bytes(index * 20)) for index in range(1, 6)],
    )
    images, count = DepthMotionLoadFrames().load(str(archive), start_frame=1, max_frames=2, stride=2)
    assert count == 2
    assert tuple(images.shape) == (2, 3, 4, 3)
    assert images.dtype.is_floating_point
    assert images[0, 0, 0, 0].item() == pytest.approx(40 / 255)
    assert images[1, 0, 0, 0].item() == pytest.approx(80 / 255)


def test_load_frames_rejects_unsafe_or_inconsistent_archives(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.zip"
    write_frames_zip(unsafe, [("../frame.png", png_bytes(10))])
    with pytest.raises(DepthMotionError, match="不安全路径"):
        DepthMotionLoadFrames().load(str(unsafe), 0, 10, 1)

    inconsistent = tmp_path / "inconsistent.zip"
    write_frames_zip(
        inconsistent,
        [("000001.png", png_bytes(10)), ("000002.png", png_bytes(20, (5, 3)))],
    )
    with pytest.raises(DepthMotionError, match="尺寸不一致"):
        DepthMotionLoadFrames().load(str(inconsistent), 0, 10, 1)


def test_load_frames_rejects_oversized_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = tmp_path / "large-batch.zip"
    write_frames_zip(archive, [("000001.png", png_bytes(10))])
    monkeypatch.setattr("comfyui_depth_motion.nodes.MAX_BATCH_PIXELS", 11)
    with pytest.raises(DepthMotionError, match="批次像素上限"):
        DepthMotionLoadFrames().load(str(archive), 0, 10, 1)
