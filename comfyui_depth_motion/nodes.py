from __future__ import annotations

from io import BytesIO
from pathlib import Path, PurePosixPath
import zipfile

import numpy as np
from PIL import Image
import torch

from .client import DepthMotionClient, DepthMotionError, PRESETS, preset_options


MAX_FRAME_COUNT = 1000
MAX_ARCHIVE_ENTRIES = 10000
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_FRAME_PIXELS = 16_777_216
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_BATCH_PIXELS = 32_000_000


class DepthMotionGenerate:
    CATEGORY = "Depth Motion"
    FUNCTION = "generate"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("depth_video", "manifest", "png_zip", "comfyui_package", "job_id")
    DESCRIPTION = "调用 Depth Motion 服务，把本地视频或公开视频链接转换为深度素材。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source": ("STRING", {"default": "", "multiline": False}),
                "service_url": ("STRING", {"default": "https://depth.whaios.com", "multiline": False}),
                "preset": (list(PRESETS.keys()), {"default": "standard_depth"}),
                "invert": ("BOOLEAN", {"default": False}),
                "output_dir": ("STRING", {"default": "output/depth-motion", "multiline": False}),
                "timeout_seconds": ("INT", {"default": 1800, "min": 30, "max": 7200, "step": 30}),
                "refresh": ("INT", {"default": 0, "min": 0, "max": 2147483647, "step": 1}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, source: str, **kwargs):
        path = Path(source).expanduser()
        if path.is_file():
            stat = path.stat()
            return (str(path.resolve()), stat.st_size, stat.st_mtime_ns, kwargs.get("refresh", 0))
        return (source.strip(), kwargs.get("refresh", 0))

    def generate(
        self,
        source: str,
        service_url: str,
        preset: str,
        invert: bool,
        output_dir: str,
        timeout_seconds: int,
        refresh: int,
    ):
        target = Path(output_dir).expanduser()
        client = DepthMotionClient(service_url, timeout=timeout_seconds)
        job = client.create_job(source.strip(), preset_options(preset, invert))
        job = client.wait(str(job["id"]))
        artifacts = client.download_artifacts(job, target)
        return (
            artifacts["download_url"],
            artifacts["manifest_url"],
            artifacts["frames_url"],
            artifacts["package_url"],
            str(job["id"]),
        )


class DepthMotionLoadFrames:
    CATEGORY = "Depth Motion"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "INT")
    RETURN_NAMES = ("images", "frame_count")
    DESCRIPTION = "从 Depth Motion PNG ZIP 加载灰度深度帧，输出 ComfyUI IMAGE 批次。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "png_zip": ("STRING", {"default": "", "multiline": False}),
                "start_frame": ("INT", {"default": 0, "min": 0, "max": 8999, "step": 1}),
                "max_frames": ("INT", {"default": 120, "min": 1, "max": MAX_FRAME_COUNT, "step": 1}),
                "stride": ("INT", {"default": 1, "min": 1, "max": 120, "step": 1}),
            }
        }

    @classmethod
    def IS_CHANGED(cls, png_zip: str, **kwargs):
        path = Path(png_zip).expanduser()
        if not path.is_file():
            return (png_zip, None)
        stat = path.stat()
        return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)

    def load(self, png_zip: str, start_frame: int, max_frames: int, stride: int):
        path = Path(png_zip).expanduser()
        if not path.is_file() or path.suffix.lower() != ".zip":
            raise DepthMotionError("PNG ZIP 文件不存在或扩展名不正确")
        frames = []
        with zipfile.ZipFile(path) as archive:
            archive_entries = archive.infolist()
            if len(archive_entries) > MAX_ARCHIVE_ENTRIES:
                raise DepthMotionError("PNG ZIP 条目数量超过上限")
            entries = []
            for info in archive_entries:
                name = PurePosixPath(info.filename)
                if info.is_dir() or name.suffix.lower() != ".png":
                    continue
                if name.is_absolute() or ".." in name.parts:
                    raise DepthMotionError("PNG ZIP 中包含不安全路径")
                entries.append(info)
            entries.sort(key=lambda item: item.filename)
            if not entries:
                raise DepthMotionError("PNG ZIP 中没有深度帧")
            selected = entries[start_frame::stride][:max_frames]
            if not selected:
                raise DepthMotionError("起始帧超出 PNG ZIP 范围")
            total_size = sum(info.file_size for info in selected)
            if len(selected) > MAX_FRAME_COUNT or total_size > MAX_UNCOMPRESSED_BYTES:
                raise DepthMotionError("所选 PNG 帧超过数量或解压大小上限")
            if any(info.file_size > MAX_FRAME_BYTES for info in selected):
                raise DepthMotionError("PNG ZIP 中包含异常大的单帧")
            expected_size = None
            total_pixels = 0
            for info in selected:
                with archive.open(info) as source:
                    content = source.read(MAX_FRAME_BYTES + 1)
                    if len(content) > MAX_FRAME_BYTES:
                        raise DepthMotionError("PNG ZIP 中包含异常大的单帧")
                    image = Image.open(BytesIO(content))
                    if image.width * image.height > MAX_FRAME_PIXELS:
                        raise DepthMotionError("PNG ZIP 中包含分辨率异常的单帧")
                    total_pixels += image.width * image.height
                    if total_pixels > MAX_BATCH_PIXELS:
                        raise DepthMotionError("所选 PNG 帧超过批次像素上限")
                    image = image.convert("RGB")
                    if expected_size is None:
                        expected_size = image.size
                    elif image.size != expected_size:
                        raise DepthMotionError("PNG ZIP 中的帧尺寸不一致")
                    frames.append(np.asarray(image, dtype=np.float32) / 255.0)
        return (torch.from_numpy(np.stack(frames)), len(frames))


NODE_CLASS_MAPPINGS = {
    "DepthMotionGenerate": DepthMotionGenerate,
    "DepthMotionLoadFrames": DepthMotionLoadFrames,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DepthMotionGenerate": "Depth Motion 生成器",
    "DepthMotionLoadFrames": "Depth Motion 加载深度帧",
}
