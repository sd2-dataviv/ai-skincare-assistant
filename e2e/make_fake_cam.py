"""Write a Y4M clip of a skin-toned, textured frame for Chrome's fake camera (no real face needed in e2e)."""
import sys

import cv2
import numpy as np

out = sys.argv[1] if len(sys.argv) > 1 else "fake-cam.y4m"
w, h = 960, 1280
rng = np.random.default_rng(7)
img = np.zeros((h, w, 3), np.uint8)
img[:] = (120, 150, 190)
img = np.clip(img + rng.normal(0, 16, img.shape), 0, 255).astype(np.uint8)
cv2.ellipse(img, (w // 2, h // 2), (w // 4, h // 3), 0, 0, 360, (130, 165, 205), -1)
for _ in range(60):
    x, y = rng.integers(w // 4, 3 * w // 4), rng.integers(h // 4, 3 * h // 4)
    cv2.circle(img, (int(x), int(y)), int(rng.integers(3, 8)), (95, 90, 150), -1)
yuv = cv2.cvtColor(img, cv2.COLOR_BGR2YUV_I420)
with open(out, "wb") as f:
    f.write(f"YUV4MPEG2 W{w} H{h} F30:1 Ip A1:1 C420jpeg\n".encode())
    for _ in range(30):
        f.write(b"FRAME\n")
        f.write(yuv.tobytes())
print(f"[e2e] fake camera written to {out}")
