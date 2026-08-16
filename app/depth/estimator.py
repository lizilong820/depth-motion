from __future__ import annotations

from functools import lru_cache
from threading import Lock

import cv2
import numpy as np
import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForDepthEstimation

from app.config import settings


class DepthEstimator:
    def __init__(self) -> None:
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.processor = AutoImageProcessor.from_pretrained(settings.model_id)
        self.model = AutoModelForDepthEstimation.from_pretrained(settings.model_id)
        self.model.to(self.device).eval()
        self._inference_lock = Lock()

    @torch.inference_mode()
    def predict(self, frame_bgr: np.ndarray, invert: bool = False) -> np.ndarray:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        with self._inference_lock:
            inputs = self.processor(images=image, return_tensors="pt")
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            output = self.model(**inputs).predicted_depth
        depth = torch.nn.functional.interpolate(
            output.unsqueeze(1),
            size=frame_bgr.shape[:2],
            mode="bicubic",
            align_corners=False,
        ).squeeze()
        depth = depth.detach().float().cpu().numpy()
        low, high = np.percentile(depth, (2, 98))
        normalized = np.clip((depth - low) / max(high - low, 1e-6), 0, 1)
        if invert:
            normalized = 1 - normalized
        return (normalized * 255).astype(np.uint8)


_model_load_lock = Lock()


@lru_cache(maxsize=1)
def _create_estimator() -> DepthEstimator:
    return DepthEstimator()


def get_estimator() -> DepthEstimator:
    with _model_load_lock:
        return _create_estimator()
