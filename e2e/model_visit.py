"""Run a consultation through the API up to the doctor's approval, with photos that have visible spots, so the skin
models (or the stand-in, scripts/fake_skin_models.py) have something to find. Prints the ids as JSON for models.mjs.

Needs the API started with LLM_PROVIDER=scripted, PHOTO_FACE_CHECK=false and ANALYSIS_ENGINE=ws.
Run with the backend's Python: backend/.venv/bin/python e2e/model_visit.py
"""
import json
import os
import sys
import uuid

import cv2
import httpx
import numpy as np

API = os.environ.get("API_URL", "http://127.0.0.1:8000").rstrip("/") + "/api"
PW = "Demo@12345"


def face(seed: int) -> bytes:
    """A skin-toned oval with a few red spots, a darker patch and shadowed under-eyes."""
    rng = np.random.default_rng(seed)
    img = np.clip(np.full((900, 900, 3), (205, 215, 225), np.float32) + rng.normal(0, 4, (900, 900, 3)), 0, 255).astype(np.uint8)
    cv2.ellipse(img, (450, 450), (250, 330), 0, 0, 360, (150, 175, 215), -1)
    img = cv2.GaussianBlur(img, (0, 0), 2)
    for x, y in [(330, 600), (370, 640), (560, 610), (600, 560), (520, 680), (300, 540)]:
        cv2.circle(img, (x + int(rng.integers(-10, 10)), y), int(rng.integers(6, 10)), (110, 120, 200), -1)
    cv2.ellipse(img, (560, 330), (45, 28), 0, 0, 360, (120, 140, 175), -1)  # a darker patch on the forehead
    for x in (360, 540):
        cv2.ellipse(img, (x, 440), (60, 18), 0, 0, 360, (125, 140, 170), -1)  # under-eye shadows
    img = cv2.GaussianBlur(img, (0, 0), 1.2)
    img = np.clip(img.astype(np.float32) + rng.normal(0, 14, img.shape), 0, 255).astype(np.uint8)  # skin texture: passes the sharpness check
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


def main() -> None:
    c = httpx.Client(base_url=API, timeout=120)
    tok = lambda e: {"Authorization": "Bearer " + c.post("/auth/login", json={"email": e, "password": PW}).json()["access_token"]}  # noqa: E731
    op = tok("operator@clinic.local")
    conv = c.post("/conversations", json={"consent_ai": True, "consent_recording": True}, headers=op).json()
    cid, ui = conv["conversation_id"], conv["ui"]

    def say(text: str) -> dict:
        r = c.post(f"/conversations/{cid}/messages", json={"text": text, "client_message_id": uuid.uuid4().hex}, headers=op)
        r.raise_for_status()
        return r.json()["ui"]

    say("My number is 98765 43210")
    say("123456")
    ui = say("I have acne and dark circles")
    for _ in range(30):
        if ui["stage"] != "assess":
            break
        q = ui["question"]
        ui = say("No" if q["id"] == "q15" else ("none" if q["type"] == "text" else ui["quick_replies"][0]))
    for i, pose in enumerate(["front", "left", "right"]):
        r = c.post(f"/conversations/{cid}/photos", data={"pose": pose},
                   files={"file": (f"{pose}.jpg", face(i + 1), "image/jpeg")}, headers=op)
        r.raise_for_status()
        ui = r.json()["ui"]
    if not ui.get("visit_id") or ui["stage"] == "capture":
        sys.exit(f"photos not accepted: {r.json()['messages'][-1]['content'] if r.json().get('messages') else ui}")
    print(json.dumps({"conversation_id": cid, "visit_id": ui["visit_id"], "stage": ui["stage"]}))


if __name__ == "__main__":
    main()
