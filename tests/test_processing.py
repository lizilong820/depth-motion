import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import cv2
import numpy as np
import pytest

from app.depth.probe import VideoProbe, probe_video, resolve_output
from app.depth.video import _scene_difference, _video_filter, process_depth_video
from app.errors import InvalidUploadError
from app.jobs.options import ProcessingOptions
from app.jobs.service import _run_depth_job
from app.jobs.store import Job, job_store


def probe() -> VideoProbe:
    return VideoProbe(
        width=1920,
        height=1080,
        fps=30,
        frames=300,
        duration=10,
        codec="h264",
        has_audio=True,
    )


def test_default_options_preserve_source_shape_and_rate() -> None:
    output = resolve_output(probe(), ProcessingOptions())
    assert output["width"] == 1920
    assert output["height"] == 1080
    assert output["fps"] == 30
    assert output["frames"] == 300


def test_output_options_resolve_clip_and_missing_dimension() -> None:
    options = ProcessingOptions(
        start_time=2,
        end_time=5,
        output_height=512,
        output_fps=12,
        fit_mode="cover",
    )
    output = resolve_output(probe(), options)
    assert output["width"] == 910
    assert output["height"] == 512
    assert output["fps"] == 12
    assert output["frames"] == 36
    assert output["duration"] == 3


def test_invalid_time_range_is_rejected() -> None:
    with pytest.raises(InvalidUploadError):
        resolve_output(probe(), ProcessingOptions(start_time=11))


def test_derived_dimension_stays_within_output_limit() -> None:
    portrait = VideoProbe(
        width=1080, height=1920, fps=30, frames=300, duration=10,
        codec="h264", has_audio=False,
    )
    output = resolve_output(portrait, ProcessingOptions(output_width=1920))
    assert output["width"] == 1080
    assert output["height"] == 1920


def test_video_filter_supports_all_fit_modes() -> None:
    assert "pad=640:640" in _video_filter(640, 640, 24, "contain")
    assert "crop=640:640" in _video_filter(640, 640, 24, "cover")
    assert _video_filter(640, 640, 24, "stretch").startswith("scale=640:640")


def test_scene_difference_detects_hard_cut() -> None:
    black = np.zeros((100, 100, 3), dtype=np.uint8)
    white = np.full((100, 100, 3), 255, dtype=np.uint8)
    _, sample = _scene_difference(None, black)
    difference, _ = _scene_difference(sample, white)
    assert difference == pytest.approx(1.0)


def test_old_job_json_gets_new_defaults(tmp_path: Path) -> None:
    job = Job.from_dict({
        "id": "old",
        "filename": "old.mp4",
        "status": "completed",
        "progress": 100,
        "message": "done",
        "created_at": "2026-01-01T00:00:00+00:00",
        "input_path": str(tmp_path / "source.mp4"),
        "output_path": str(tmp_path / "depth.mp4"),
    })
    assert job.options == {}
    assert job.source_platform is None
    assert job.public()["manifest_url"] is None


def test_full_comfyui_package_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "depth.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
    ], check=True, capture_output=True)

    class FakeEstimator:
        device = "test"

        def predict_raw(self, frame: np.ndarray) -> np.ndarray:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

    monkeypatch.setattr("app.depth.estimator.get_estimator", lambda: FakeEstimator())
    metadata = process_depth_video(
        source_path=source,
        output_path=output,
        work_dir=tmp_path,
        options=ProcessingOptions(
            start_time=0.5,
            end_time=1.5,
            output_width=256,
            output_height=256,
            output_fps=8,
            fit_mode="contain",
            export_png=True,
            create_package=True,
        ),
        progress=lambda frame, total: None,
        source_platform="test",
    )

    assert metadata["frames"] == 8
    output_probe = probe_video(output)
    assert (output_probe.width, output_probe.height) == (256, 256)
    assert output_probe.fps == pytest.approx(8)
    assert output_probe.has_audio

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["output"]["frames"] == 8
    assert manifest["source"]["platform"] == "test"
    assert manifest["package_contents"] == [
        "depth.mp4", "source.mp4", "manifest.json",
        *[f"depth-frames/{index:06d}.png" for index in range(1, 9)],
    ]
    depth_artifact = next(item for item in manifest["artifacts"] if item["type"] == "depth_mp4")
    assert depth_artifact["size"] == output.stat().st_size
    assert depth_artifact["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()

    expected_frames = [f"depth-frames/{index:06d}.png" for index in range(1, 9)]
    with zipfile.ZipFile(tmp_path / "depth-frames.zip") as archive:
        assert archive.namelist() == expected_frames
    with zipfile.ZipFile(tmp_path / "comfyui-package.zip") as archive:
        assert archive.namelist() == ["depth.mp4", "source.mp4", "manifest.json", *expected_frames]
        assert json.loads(archive.read("manifest.json")) == manifest
        package_source = tmp_path / "package-source.mp4"
        package_source.write_bytes(archive.read("source.mp4"))
    assert probe_video(package_source).has_audio
    assert not (tmp_path / "source-normalized.mp4").exists()
    assert not (tmp_path / "depth-frames").exists()


def test_failed_job_removes_partial_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    job = job_store.create(filename="broken.mp4", suffix=".mp4")
    try:
        work_dir = job.directory
        for filename in (
            "source-normalized.mp4", "depth-silent.mp4", "depth.mp4",
            "manifest.json", "depth-frames.zip.tmp", "comfyui-package.zip.tmp",
        ):
            (work_dir / filename).write_bytes(b"partial")
        frames_dir = work_dir / "depth-frames"
        frames_dir.mkdir()
        (frames_dir / "000001.png").write_bytes(b"partial")

        monkeypatch.setattr(
            "app.jobs.service.process_depth_video",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("expected failure")),
        )
        _run_depth_job(job.id, release_slot=False)

        failed = job_store.get(job.id)
        assert failed.status == "failed"
        assert failed.error == "深度视频处理失败"
        assert not (work_dir / "source-normalized.mp4").exists()
        assert not (work_dir / "depth-silent.mp4").exists()
        assert not Path(job.output_path).exists()
        assert not (work_dir / "manifest.json").exists()
        assert not frames_dir.exists()
    finally:
        job_store.delete(job.id)
        shutil.rmtree(job.directory, ignore_errors=True)
