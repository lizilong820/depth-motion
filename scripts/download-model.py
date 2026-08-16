from __future__ import annotations

import argparse
from pathlib import Path

from transformers import AutoImageProcessor, AutoModelForDepthEstimation


def main() -> None:
    parser = argparse.ArgumentParser(description="Download the depth model for offline serving")
    parser.add_argument("--model-id", default="depth-anything/Depth-Anything-V2-Small-hf")
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parent.parent / "model")
    args = parser.parse_args()

    args.destination.mkdir(parents=True, exist_ok=True)
    processor = AutoImageProcessor.from_pretrained(args.model_id)
    model = AutoModelForDepthEstimation.from_pretrained(args.model_id)
    processor.save_pretrained(args.destination)
    model.save_pretrained(args.destination, safe_serialization=True)
    print(f"Model saved to {args.destination}")


if __name__ == "__main__":
    main()
