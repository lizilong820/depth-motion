from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.config import ROOT_DIR


router = APIRouter(prefix="/api", tags=["comfyui"])
NODE_PACKAGE_DIR = ROOT_DIR / "comfyui_depth_motion"
NODE_PACKAGE_FILES = ("__init__.py", "client.py", "nodes.py", "requirements.txt", "README.md")


@router.get("/comfyui-node")
def download_comfyui_node() -> StreamingResponse:
    archive_buffer = BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename in NODE_PACKAGE_FILES:
            source = NODE_PACKAGE_DIR / filename
            if not source.is_file():
                raise RuntimeError(f"ComfyUI 节点包缺少文件：{filename}")
            archive.write(source, f"comfyui_depth_motion/{filename}")
    archive_buffer.seek(0)
    return StreamingResponse(
        archive_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="comfyui_depth_motion.zip"'},
    )
