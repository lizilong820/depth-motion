from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT_DIR, settings
from app.errors import AppError
from app.jobs.router import router as jobs_router


logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
)
logger = logging.getLogger("depth-studio")

app = FastAPI(title="Depth Motion Studio", version="0.1.0")
app.include_router(jobs_router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id", uuid4().hex)
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["x-frame-options"] = "DENY"
    response.headers["referrer-policy"] = "no-referrer"
    return response


@app.exception_handler(AppError)
async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
    request_id = request.headers.get("x-request-id", "")
    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.code, "message": error.message, "request_id": request_id},
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
    request_id = request.headers.get("x-request-id", "")
    logger.exception("unexpected_error request_id=%s", request_id)
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL_ERROR", "message": "服务器处理失败", "request_id": request_id},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": settings.model_id}


static_dir = ROOT_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
