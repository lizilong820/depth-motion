from __future__ import annotations

from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse

import requests


PRESETS = {
    "quick_preview": {
        "max_output_side": 768,
        "max_output_fps": 12,
        "temporal_smoothing": 0.20,
        "stabilize_range": 0.78,
        "export_png": False,
        "create_package": False,
    },
    "standard_depth": {
        "max_output_side": 1280,
        "max_output_fps": 24,
        "temporal_smoothing": 0.25,
        "stabilize_range": 0.85,
        "export_png": False,
        "create_package": False,
    },
    "motion_character": {
        "max_output_side": 1024,
        "max_output_fps": 24,
        "temporal_smoothing": 0.32,
        "stabilize_range": 0.90,
        "export_png": False,
        "create_package": False,
    },
    "comfyui_package": {
        "max_output_side": 1280,
        "max_output_fps": 24,
        "temporal_smoothing": 0.25,
        "stabilize_range": 0.85,
        "export_png": True,
        "create_package": True,
    },
    "high_quality_png": {
        "max_output_side": 1920,
        "max_output_fps": 30,
        "temporal_smoothing": 0.20,
        "stabilize_range": 0.82,
        "export_png": True,
        "create_package": False,
    },
}
ARTIFACTS = {
    "download_url": "depth.mp4",
    "manifest_url": "manifest.json",
    "frames_url": "depth-frames.zip",
    "package_url": "comfyui-package.zip",
}
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024


class DepthMotionError(RuntimeError):
    pass


def _service_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DepthMotionError("服务地址必须是只包含协议和主机的 HTTP/HTTPS URL")
    return value.strip().rstrip("/")


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return name[:80] or "depth-motion"


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Depth Motion 请求失败（HTTP {response.status_code}）"
    if not isinstance(payload, dict):
        return f"Depth Motion 请求失败（HTTP {response.status_code}）"
    return str(payload.get("message") or f"Depth Motion 请求失败（HTTP {response.status_code}）")


class DepthMotionClient:
    def __init__(self, service_url: str, timeout: int = 1800, poll_interval: float = 1.0) -> None:
        self.service_url = _service_url(service_url)
        self.timeout = timeout
        self.poll_interval = max(0.2, poll_interval)
        self.session = requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", (20, self.timeout))
        kwargs.setdefault("allow_redirects", False)
        try:
            response = self.session.request(
                method,
                urljoin(f"{self.service_url}/", path.lstrip("/")),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise DepthMotionError(f"无法连接 Depth Motion 服务：{exc}") from exc
        if 300 <= response.status_code < 400:
            raise DepthMotionError("Depth Motion 服务返回了不允许的重定向")
        if not response.ok:
            raise DepthMotionError(_error_message(response))
        return response

    def create_job(self, source: str, options: dict) -> dict:
        source_path = Path(source).expanduser()
        if source_path.is_file():
            with source_path.open("rb") as video:
                response = self._request(
                    "POST",
                    "/api/jobs",
                    data={key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in options.items()},
                    files={"file": (source_path.name, video, "video/mp4")},
                )
        elif source.startswith(("http://", "https://")):
            response = self._request("POST", "/api/jobs/remote", json={"url": source, **options})
        else:
            raise DepthMotionError("视频来源必须是本地文件路径或 HTTP/HTTPS 视频链接")
        return response.json()

    def wait(self, job_id: str) -> dict:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            job = self._request("GET", f"/api/jobs/{job_id}").json()
            if job.get("status") == "completed":
                return job
            if job.get("status") == "failed":
                raise DepthMotionError(str(job.get("error") or "深度视频生成失败"))
            time.sleep(self.poll_interval)
        raise DepthMotionError(f"任务等待超过 {self.timeout} 秒")

    def download_artifacts(self, job: dict, output_dir: Path) -> dict[str, str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        job_dir = output_dir / _safe_name(str(job.get("id") or "depth-motion"))
        job_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}
        expected_origin = urlparse(self.service_url)
        for field, filename in ARTIFACTS.items():
            artifact_url = job.get(field)
            if not artifact_url:
                paths[field] = ""
                continue
            absolute_url = urljoin(f"{self.service_url}/", str(artifact_url).lstrip("/"))
            parsed = urlparse(absolute_url)
            if (parsed.scheme, parsed.netloc) != (expected_origin.scheme, expected_origin.netloc):
                raise DepthMotionError("服务器返回了非同源产物地址")
            destination = job_dir / filename
            temporary = destination.with_suffix(destination.suffix + ".part")
            try:
                with self._request("GET", absolute_url, stream=True) as response, temporary.open("wb") as target:
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_ARTIFACT_BYTES:
                        raise DepthMotionError("Depth Motion 产物超过下载大小上限")
                    downloaded = 0
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            downloaded += len(chunk)
                            if downloaded > MAX_ARTIFACT_BYTES:
                                raise DepthMotionError("Depth Motion 产物超过下载大小上限")
                            target.write(chunk)
                temporary.replace(destination)
            except requests.RequestException as exc:
                temporary.unlink(missing_ok=True)
                raise DepthMotionError(f"下载 Depth Motion 产物失败：{exc}") from exc
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            paths[field] = str(destination.resolve())
        return paths


def preset_options(preset: str, invert: bool) -> dict:
    if preset not in PRESETS:
        raise DepthMotionError(f"不支持的预设：{preset}")
    return {
        "preset": preset,
        "invert": invert,
        "fit_mode": "contain",
        "scene_cut_reset": True,
        "scene_cut_threshold": 0.32,
        **PRESETS[preset],
    }
