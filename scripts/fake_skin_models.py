"""STAND-IN for the clinic's skin model servers, for development and demos when the real ones (Detectron2 + LaMa on
the GPU machine) aren't reachable. It speaks the same WebSocket protocol, including the opt-in findings message, but
its "findings" come from simple image processing (dark or reddish spots on the face), NOT a trained model. Never use
it for real clients.

    backend/.venv/bin/python scripts/fake_skin_models.py            # diagnosis on :9711, post-treatment on :9710
    python scripts/fake_skin_models.py --no-face-check               # synthetic test images without a real face

Then in backend/.env:
    ANALYSIS_ENGINE=ws
    SKIN_DIAGNOSIS_WS_URL=ws://127.0.0.1:9711/ws/cv/1/image/image
    SKIN_POST_TREATMENT_WS_URL=ws://127.0.0.1:9710/ws/cv/2/image/image
"""
import argparse
import asyncio
import json

import cv2
import numpy as np
from websockets.asyncio.server import serve

CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml") if hasattr(cv2, "CascadeClassifier") else None
DEFAULT_COLORS = {"acne": [255, 0, 0], "scars": [0, 255, 0], "pigmentation": [0, 0, 255], "dark circles": [255, 255, 0]}


def unwrap(p: dict) -> dict:
    if isinstance(p, dict) and isinstance(p.get("parameters"), dict):
        return {**{k: v for k, v in p.items() if k != "parameters"}, **p["parameters"]}
    return p if isinstance(p, dict) else {}


def face_crop(img: np.ndarray, params: dict, face_check: bool):
    h, w = img.shape[:2]
    faces = CASCADE.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 6, minSize=(53, 53)) if face_check and CASCADE is not None else []
    if len(faces):
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        m = 30
        box = [max(0, x - m), max(0, y - m), min(w, x + fw + m), min(h, y + fh + m)]
    elif not face_check:  # synthetic images: take the middle of the picture as the face
        box = [int(w * 0.2), int(h * 0.12), int(w * 0.8), int(h * 0.88)]
    else:
        return None, None
    size = tuple(int(v) for v in (params.get("output_image_size") or (1024, 1024)))
    crop = img[box[1]:box[3], box[0]:box[2]]
    return cv2.resize(crop, size), [int(v) for v in box]


def find_spots(face: np.ndarray, params: dict) -> tuple[list[dict], np.ndarray]:
    """Small dark/red spots -> acne, larger flat dark patches -> pigmentation, darker band under the eyes -> dark
    circles. Only inside an oval for the face, so hair, background and the jaw line are left out. Returns the
    detections and the union mask."""
    h, w = face.shape[:2]
    lab = cv2.cvtColor(face, cv2.COLOR_BGR2LAB).astype(np.float32)
    L = cv2.GaussianBlur(lab[..., 0], (0, 0), max(1.5, w / 600))  # ignore pore-level texture
    a = cv2.GaussianBlur(lab[..., 1], (0, 0), max(1.5, w / 600))
    local_L = cv2.GaussianBlur(L, (0, 0), w / 12)
    local_a = cv2.GaussianBlur(a, (0, 0), w / 12)
    inside = np.zeros((h, w), np.uint8)
    cv2.ellipse(inside, (w // 2, int(h * 0.52)), (int(w * 0.36), int(h * 0.40)), 0, 0, 360, 255, -1)
    eyes = np.zeros((h, w), np.uint8)  # the under-eye bands are judged separately as dark circles
    for x0 in (int(w * 0.18), int(w * 0.56)):
        eyes[int(h * 0.38):int(h * 0.52), x0:x0 + int(w * 0.26)] = 255
    spots = (((local_L - L) > 9) | ((a - local_a) > 7)).astype(np.uint8) * 255
    spots = cv2.bitwise_and(spots, cv2.bitwise_and(inside, cv2.bitwise_not(eyes)))
    spots = cv2.morphologyEx(spots, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(spots)
    dets, union = [], np.zeros((h, w), np.uint8)
    classes = params.get("classes") or list(DEFAULT_COLORS)
    threshold = float(params.get("threshold", 0.5))
    area_min, acne_max = (w * h) * 0.00012, (w * h) * 0.0025
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < area_min or area > (w * h) * 0.04:
            continue
        cls = "acne" if area <= acne_max else "pigmentation"
        region = labels == i
        contrast = max(float((local_L - L)[region].mean()), float((a - local_a)[region].mean()))
        score = round(float(np.clip(0.3 + contrast / 30, 0.05, 0.99)), 3)
        if cls in classes and score > threshold:
            dets.append({"bbox": [x, y, x + bw, y + bh], "class": cls, "score": score, "area_px": area})
            union[region] = 255
    if "dark circles" in classes:  # under-eye band vs the cheek just below it
        for x0 in (int(w * 0.18), int(w * 0.56)):
            band = (slice(int(h * 0.40), int(h * 0.50)), slice(x0, x0 + int(w * 0.26)))
            cheek = (slice(int(h * 0.56), int(h * 0.64)), slice(x0, x0 + int(w * 0.26)))
            diff = float(L[cheek].mean() - L[band].mean())
            score = round(float(np.clip(0.3 + diff / 25, 0.05, 0.95)), 3)
            if score > threshold:
                y0, y1 = band[0].start, band[0].stop
                band_dark = cv2.dilate(((L[band] < L[cheek].mean() - 6).astype(np.uint8)) * 255, np.ones((9, 9), np.uint8))
                area = int((band_dark > 0).sum())
                dets.append({"bbox": [x0, y0, x0 + int(w * 0.26), y1], "class": "dark circles", "score": score, "area_px": area})
                union[band] = np.maximum(union[band], band_dark)
    return dets, union


def diagnose(img: np.ndarray, params: dict, face_check: bool):
    face, box = face_crop(img, params, face_check)
    if face is None:
        return img, {"model": "stand-in diagnosis", "face_detected": False, "face_box": None, "detections": []}
    dets, _ = find_spots(face, params)
    out = face.copy()
    colors = params.get("bbox_colors") or DEFAULT_COLORS
    thick = int(params.get("bbox_thickness", 2))
    if params.get("draw_bbox", True):
        for d in dets:
            r, g, b = colors.get(d["class"], [255, 0, 0])
            x1, y1, x2, y2 = d["bbox"]
            cv2.rectangle(out, (x1, y1), (x2, y2), (b, g, r), thick)
            cv2.putText(out, f"{d['class']} {d['score']:.0%}", (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (b, g, r), 1, cv2.LINE_AA)
    cv2.putText(out, "STAND-IN MODEL", (8, out.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    size = [out.shape[1], out.shape[0]]
    return out, {"model": "stand-in diagnosis", "face_detected": True, "face_box": box, "input_size": [img.shape[1], img.shape[0]],
                 "image_size": size, "output_size": size, "detections": dets}


def post_treatment(img: np.ndarray, params: dict, face_check: bool):
    face, box = face_crop(img, params, face_check)
    if face is None:
        return img, {"model": "stand-in post-treatment", "face_detected": False, "face_box": None, "detections": [],
                     "inpainted": False}
    dets, mask = find_spots(face, params)
    dil = int(params.get("mask_dilate", 0))
    if dil > 0:
        mask = cv2.dilate(mask, np.ones((dil, dil), np.uint8))
    out = cv2.inpaint(face, mask, 5, cv2.INPAINT_TELEA) if dets else face
    size = [out.shape[1], out.shape[0]]
    return out, {"model": "stand-in post-treatment", "face_detected": True, "face_box": box, "image_size": size,
                 "output_size": size, "detections": dets, "inpainted": bool(dets)}


def handler_for(fn, face_check: bool, delay: float):
    async def handler(ws):
        params: dict = {}
        async for msg in ws:
            if isinstance(msg, str):
                params = unwrap(json.loads(msg))
                continue
            img = cv2.imdecode(np.frombuffer(msg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                await ws.send(msg)
                continue
            if delay:
                await asyncio.sleep(delay)
            out, details = fn(img, params, face_check)
            await ws.send(cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes())
            if params.get("return_predictions"):
                await ws.send(json.dumps({"type": "predictions", **details}))
    return handler


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--diag-port", type=int, default=9711)
    ap.add_argument("--post-port", type=int, default=9710)
    ap.add_argument("--no-face-check", action="store_true", help="treat the middle of the picture as the face")
    ap.add_argument("--post-delay", type=float, default=2.0, help="seconds, to mimic slow inpainting")
    a = ap.parse_args()
    fc = not a.no_face_check
    async with serve(handler_for(diagnose, fc, 0), a.host, a.diag_port, max_size=64 * 2**20), \
            serve(handler_for(post_treatment, fc, a.post_delay), a.host, a.post_port, max_size=64 * 2**20):
        print(f"STAND-IN skin models: diagnosis ws://{a.host}:{a.diag_port}/ws/cv/1/image/image, "
              f"post-treatment ws://{a.host}:{a.post_port}/ws/cv/2/image/image (face check {'on' if fc else 'off'})",
              flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
