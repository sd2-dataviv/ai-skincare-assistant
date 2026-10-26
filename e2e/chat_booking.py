"""Make a tentative treatment booking the way a client does: a full consultation in the chat, doctor approval, then
tapping the first offered slot. Prints the booking id and day as JSON (used by e2e/ot.mjs).

Needs the API on :8000 started with PHOTO_FACE_CHECK=false and LLM_PROVIDER=scripted (make api-demo).
Run with the backend's Python: backend/.venv/bin/python e2e/chat_booking.py
"""
import json
import sys
import uuid

import cv2
import httpx
import numpy as np

API = "http://127.0.0.1:8000/api"
PW = "Demo@12345"


def jpeg(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    img = np.clip(np.full((800, 800, 3), (110, 140, 170), np.float32) + rng.normal(0, 18, (800, 800, 3)), 0, 255).astype(np.uint8)
    cv2.ellipse(img, (400, 400), (200, 266), 0, 0, 360, (120, 150, 180), -1)
    return cv2.imencode(".jpg", img)[1].tobytes()


def main() -> None:
    c = httpx.Client(base_url=API, timeout=60)
    tok = lambda e: {"Authorization": "Bearer " + c.post("/auth/login", json={"email": e, "password": PW}).json()["access_token"]}  # noqa: E731
    op, dr = tok("operator@clinic.local"), tok("doctor@clinic.local")
    conv = c.post("/conversations", json={"consent_ai": True, "consent_recording": True}, headers=op).json()
    cid, ui = conv["conversation_id"], conv["ui"]

    def say(text: str) -> dict:
        r = c.post(f"/conversations/{cid}/messages", json={"text": text, "client_message_id": uuid.uuid4().hex}, headers=op)
        r.raise_for_status()
        return r.json()["ui"]

    say("My number is 98765 43210")
    say("123456")
    ui = say("I want help with dark spots on my cheeks")
    for _ in range(30):
        if ui["stage"] != "assess":
            break
        q = ui["question"]
        ui = say("No" if q["id"] == "q15" else ("none" if q["type"] == "text" else ui["quick_replies"][0]))
    for i, pose in enumerate(["front", "left", "right"]):
        r = c.post(f"/conversations/{cid}/photos", data={"pose": pose},
                   files={"file": (f"{pose}.jpg", jpeg(i + 1), "image/jpeg")}, headers=op)
        r.raise_for_status()
        ui = r.json()["ui"]
    vid = ui["visit_id"]
    r = c.post(f"/visits/{vid}/approval/decision", json={"decision": "edit", "items": [
        {"catalogue_id": "svc-qswitch-toning", "sessions": 6}, {"catalogue_id": "prd-sunscreen"}]}, headers=dr)
    r.raise_for_status()
    ui = c.get(f"/conversations/{cid}", headers=op).json()["ui"]
    if ui["stage"] != "booking":
        sys.exit(f"expected the booking step, got {ui['stage']}")
    slot = ui["booking"]["slots"][0]
    say(slot["label"])
    booking = [b for b in c.get(f"/ot/visits/{vid}/bookings", headers=dr).json() if b["status"] == "tentative"][0]
    print(json.dumps({"booking_id": booking["id"], "starts_at": booking["starts_at"], "label": booking["label"],
                      "visit_id": vid}))


if __name__ == "__main__":
    main()
