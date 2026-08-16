from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from app.errors import InvalidUploadError


FitMode = Literal["contain", "cover", "stretch"]


@dataclass(frozen=True)
class ProcessingOptions:
    invert: bool = False
    temporal_smoothing: float = 0.25
    start_time: float = 0.0
    end_time: float | None = None
    output_width: int | None = None
    output_height: int | None = None
    output_fps: float | None = None
    fit_mode: FitMode = "contain"
    export_png: bool = False
    create_package: bool = False
    stabilize_range: float = 0.85
    scene_cut_reset: bool = True
    scene_cut_threshold: float = 0.32

    def public(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict | None) -> "ProcessingOptions":
        if not isinstance(value, dict):
            return cls()
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: item for key, item in value.items() if key in allowed})

    def validate(self) -> None:
        if not 0 <= self.temporal_smoothing <= 0.9:
            raise InvalidUploadError("时序稳定度必须在 0 到 0.9 之间")
        if self.start_time < 0:
            raise InvalidUploadError("开始时间不能小于 0")
        if self.end_time is not None and self.end_time <= self.start_time:
            raise InvalidUploadError("结束时间必须晚于开始时间")
        for value, name in ((self.output_width, "输出宽度"), (self.output_height, "输出高度")):
            if value is not None and not 128 <= value <= 1920:
                raise InvalidUploadError(f"{name}必须在 128 到 1920 之间")
        if self.output_fps is not None and not 1 <= self.output_fps <= 60:
            raise InvalidUploadError("输出帧率必须在 1 到 60 FPS 之间")
        if self.fit_mode not in {"contain", "cover", "stretch"}:
            raise InvalidUploadError("输出适配模式不正确")
        if not 0 <= self.stabilize_range <= 0.98:
            raise InvalidUploadError("深度范围稳定度必须在 0 到 0.98 之间")
        if not 0.05 <= self.scene_cut_threshold <= 1:
            raise InvalidUploadError("切镜阈值必须在 0.05 到 1 之间")
