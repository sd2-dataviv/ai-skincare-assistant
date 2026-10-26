"""Guided capture: per-photo quality gates (face, framing, light, sharpness) and storage."""
import logging
from functools import lru_cache

import cv2
import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.integrations.storage import get_storage
from app.models import Photo, Visit
from app.services import DomainError

log = logging.getLogger(__name__)

REQUIRED_POSES = ["front", "left", "right"]
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

POSE_PROMPTS = {
    "front": "Look straight at the camera with a neutral expression.",
    "left": "Turn your head to your right so I can see your left cheek.",
    "right": "Now turn your head to your left so I can see your right cheek.",
}

GUIDANCE = {
    "unreadable": "I couldn't read that photo. Let's try again.",
    "too_small": "That photo is too small. Please use the main camera.",
    "too_dark": "It's a little dark. Please face a window or a light.",
    "too_bright": "There's too much glare. Please step out of direct light.",
    "blurry": "The photo is blurry. Please hold still for a second.",
    "no_face": "I can't see your face clearly. Please position it inside the oval.",
    "too_far": "Please move a little closer.",
    "too_close": "Please move back a little.",
    "off_centre": "Please centre your face in the oval.",
    "check_unavailable": "I can't check photos right now. Please ask the clinic staff for help.",
}


@lru_cache
def _cascades():
    """Load the face detectors once. Returns None if this OpenCV build has no CascadeClassifier (e.g. 5.x)."""
    if not hasattr(cv2, "CascadeClassifier"):
        log.error("Face check unavailable: this OpenCV build has no CascadeClassifier (install opencv-python-headless<5)")
        return None
    base = cv2.data.haarcascades
    frontal = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")
    if frontal.empty() or profile.empty():
        log.error("Face check unavailable: Haar cascade files not found in %s", base)
        return None
    return frontal, profile


def face_check_available() -> bool:
    return _cascades() is not None


def _detect_face(gray: np.ndarray, pose: str):
    frontal, profile = _cascades()
    min_size = (gray.shape[1] // 8, gray.shape[1] // 8)
    faces = list(frontal.detectMultiScale(gray, 1.1, 5, minSize=min_size))
    if pose != "front":
        faces += list(profile.detectMultiScale(gray, 1.1, 4, minSize=min_size))
        flipped = cv2.flip(gray, 1)
        w = gray.shape[1]
        for (x, y, fw, fh) in profile.detectMultiScale(flipped, 1.1, 4, minSize=min_size):
            faces.append((w - x - fw, y, fw, fh))
    if not faces:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def quality_check(data: bytes, pose: str) -> dict:
    s = get_settings()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return {"ok": False, "issues": ["unreadable"], "guidance": GUIDANCE["unreadable"], "metrics": {}}
    h, w = img.shape[:2]
    issues: list[str] = []
    if min(h, w) < s.photo_min_side:
        issues.append("too_small")
    scale = 640 / w if w > 640 else 1.0
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale != 1.0 else img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if brightness < 70:
        issues.append("too_dark")
    elif brightness > 210:
        issues.append("too_bright")
    if sharpness < s.photo_min_sharpness:
        issues.append("blurry")
    face_ratio = None
    if s.photo_face_check and not face_check_available():
        issues.append("check_unavailable")
    elif s.photo_face_check:
        face = _detect_face(gray, pose)
        if face is None:
            issues.append("no_face")
        else:
            x, y, fw, fh = face
            gw = gray.shape[1]
            face_ratio = round(fw / gw, 3)
            if face_ratio < 0.2:
                issues.append("too_far")
            elif face_ratio > 0.75:
                issues.append("too_close")
            elif abs((x + fw / 2) - gw / 2) / gw > 0.22:
                issues.append("off_centre")
    metrics = {"width": w, "height": h, "brightness": round(brightness, 1), "sharpness": round(sharpness, 1),
               "face_ratio": face_ratio}
    ok = not issues
    return {"ok": ok, "issues": issues, "guidance": None if ok else GUIDANCE[issues[0]], "metrics": metrics}


async def save_photo(db: AsyncSession, visit: Visit, pose: str, data: bytes, content_type: str) -> Photo:
    s = get_settings()
    if pose not in REQUIRED_POSES:
        raise DomainError("invalid_pose", f"Pose must be one of {', '.join(REQUIRED_POSES)}.", 422)
    if content_type not in ALLOWED_TYPES:
        raise DomainError("invalid_type", "Photos must be JPEG, PNG or WebP.", 415)
    if len(data) > s.photo_max_bytes:
        raise DomainError("too_large", "Photo is larger than 10 MB.", 413)
    q = quality_check(data, pose)
    key = await get_storage().put(data, ALLOWED_TYPES[content_type])
    photo = Photo(
        visit_id=visit.id, pose=pose, storage_key=key, content_type=content_type,
        width=q["metrics"].get("width", 0), height=q["metrics"].get("height", 0), quality=q, accepted=q["ok"],
    )
    db.add(photo)
    await db.flush()
    return photo


def accepted_by_pose(photos: list[Photo]) -> dict[str, Photo]:
    out: dict[str, Photo] = {}
    for p in photos:  # latest accepted photo per pose wins
        if p.accepted:
            out[p.pose] = p
    return out


def missing_poses(photos: list[Photo]) -> list[str]:
    have = accepted_by_pose(photos)
    return [p for p in REQUIRED_POSES if p not in have]
