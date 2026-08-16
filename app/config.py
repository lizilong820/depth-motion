from dataclasses import dataclass
from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "500"))
    model_id: str = os.getenv(
        "DEPTH_MODEL_ID", "depth-anything/Depth-Anything-V2-Small-hf"
    )
    inference_size: int = int(os.getenv("INFERENCE_SIZE", "518"))

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


settings = Settings()
settings.jobs_dir.mkdir(parents=True, exist_ok=True)
