"""Write three synthetic face photos (front, left, right) for testing without a camera.

They pass the brightness, sharpness and size checks but contain no real face, so run the API with
PHOTO_FACE_CHECK=false (`make api-nocam` or `make api-demo`). Usage: python -m scripts.sample_photos [out_dir]
"""
import sys
from pathlib import Path

import cv2
import numpy as np


def make(pose: str, seed: int, size: int = 900) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.full((size, size, 3), (205, 215, 225), np.uint8)  # light background (BGR)
    img = np.clip(img + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    cx = {"front": size // 2, "left": size // 2 + 60, "right": size // 2 - 60}[pose]
    cy = size // 2
    skin = (120, 160, 205)
    cv2.ellipse(img, (cx, cy), (size // 4 if pose == "front" else size // 5, size // 3), 0, 0, 360, skin, -1)
    # Skin texture, a few spots (acne) and some darker patches (pigmentation)
    tex = rng.normal(0, 10, img.shape)
    mask = np.zeros(img.shape[:2], np.uint8)
    cv2.ellipse(mask, (cx, cy), (size // 4, size // 3), 0, 0, 360, 255, -1)
    img = np.where(mask[..., None] > 0, np.clip(img + tex, 0, 255), img).astype(np.uint8)
    for _ in range(35):
        x, y = rng.integers(cx - size // 6, cx + size // 6), rng.integers(cy - size // 5, cy + size // 4)
        cv2.circle(img, (int(x), int(y)), int(rng.integers(3, 8)), (80, 85, 175), -1)
    for _ in range(4):
        x, y = rng.integers(cx - size // 6, cx + size // 6), rng.integers(cy - size // 10, cy + size // 8)
        cv2.ellipse(img, (int(x), int(y)), (22, 14), 0, 0, 360, (95, 125, 165), -1)
    # Simple features so it reads as a face to a person looking at it
    eye_dx = 70 if pose == "front" else 50
    for dx in (-eye_dx, eye_dx):
        if (pose == "left" and dx < 0) or (pose == "right" and dx > 0):
            continue
        cv2.ellipse(img, (cx + dx, cy - 70), (28, 12), 0, 0, 360, (60, 50, 40), -1)
    cv2.ellipse(img, (cx, cy + 120), (55, 14), 0, 0, 180, (70, 70, 150), 4)
    return img


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(parents=True, exist_ok=True)
    for i, pose in enumerate(("front", "left", "right"), start=1):
        path = out / f"{i}-{pose}.jpg"
        cv2.imwrite(str(path), make(pose, seed=i), [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"[samples] wrote {path}")


if __name__ == "__main__":
    main()
