"""Skin analysis engine adapter (diagnosis + anti-ageing).

`MockAnalysisEngine` derives plausible, deterministic scores from simple image statistics so the full flow
can be exercised end-to-end. It is NOT a medical model; results are labelled engine="mock".
`HttpAnalysisEngine` posts the photos to the clinic's existing analysis service.
`SkinModelEngine` (ANALYSIS_ENGINE=ws) sends each photo to the clinic's skin diagnosis model over WebSocket. Acne,
pigmentation and dark circles are scored from what the model finds; the other concerns stay estimates (mock) until a
model covers them, and every score says which it is. If the model can't be reached, the whole result falls back to the
estimates and says so.
"""
import hashlib
from dataclasses import dataclass, field
from typing import Protocol

import cv2
import httpx
import numpy as np

from app.config import get_settings
from app.integrations import with_retries

CONCERNS = ["acne", "pigmentation", "wrinkles", "dark_circles", "redness", "pores", "dullness", "dryness"]


def severity_label(score: float) -> str:
    if score < 25:
        return "minimal"
    if score < 45:
        return "mild"
    if score < 70:
        return "moderate"
    return "severe"


@dataclass
class EngineImage:
    """A picture a model made from one of the photos (e.g. the photo with what it found marked on it)."""
    kind: str  # diagnosis_overlay
    pose: str
    data: bytes
    content_type: str
    model: str
    details: dict


@dataclass
class EngineResult:
    engine: str
    diagnosis: dict[str, dict]  # concern -> {score, label, source?}
    anti_aging: dict
    confidence: float
    flags: list[dict] = field(default_factory=list)  # [{type: "non_skincare", note: "..."}]
    images: list[EngineImage] = field(default_factory=list)
    model: dict | None = None  # what the skin model did: status, message, per-photo findings, extra findings


class AnalysisEngine(Protocol):
    async def analyse(self, images: dict[str, bytes], age: int | None) -> EngineResult: ...


class MockAnalysisEngine:
    name = "mock-v1"

    async def analyse(self, images: dict[str, bytes], age: int | None) -> EngineResult:
        front = images.get("front") or next(iter(images.values()))
        img = cv2.imdecode(np.frombuffer(front, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Unreadable image")
        h, w = img.shape[:2]
        crop = img[int(h * 0.15): int(h * 0.85), int(w * 0.2): int(w * 0.8)]
        lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB).astype(np.float32)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        L, a = lab[..., 0], lab[..., 1]
        lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(L.mean())
        unevenness = float(L.std())
        redness = float(a.mean() - 128)

        seed = int(hashlib.sha256(front).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)

        def score(base: float) -> float:
            return float(np.clip(base + rng.normal(0, 8), 3, 92))

        raw = {
            "acne": 20 + min(lap / 40, 30),
            "pigmentation": 15 + min(unevenness * 0.9, 45),
            "wrinkles": 10 + (max(0, (age or 30) - 25) * 1.4) + min(lap / 80, 15),
            "dark_circles": 25 + max(0, 120 - brightness) * 0.25,
            "redness": 15 + max(0, redness) * 2.2,
            "pores": 20 + min(lap / 50, 30),
            "dullness": 20 + max(0, 140 - brightness) * 0.3,
            "dryness": 25 + rng.uniform(0, 20),
        }
        diagnosis = {}
        for c in CONCERNS:
            s = round(score(raw[c]), 1)
            diagnosis[c] = {"score": s, "label": severity_label(s)}

        skin_age = None
        if age is not None:
            skin_age = int(round(age + (diagnosis["wrinkles"]["score"] - 35) / 6 + rng.normal(0, 1.5)))
        anti_aging = {
            "skin_age": skin_age,
            "wrinkle_index": diagnosis["wrinkles"]["score"],
            "firmness": round(float(np.clip(100 - diagnosis["wrinkles"]["score"] * 0.8 + rng.normal(0, 5), 20, 98)), 1),
        }
        # Confidence falls with poor light or blur, as a real model's would.
        conf = 0.93
        if brightness < 70 or brightness > 210:
            conf -= 0.2
        if lap < 30:
            conf -= 0.2
        return EngineResult(self.name, diagnosis, anti_aging, round(conf, 2), [])


class HttpAnalysisEngine:
    def __init__(self, url: str):
        self.url = url

    async def analyse(self, images: dict[str, bytes], age: int | None) -> EngineResult:
        async def call():
            files = {pose: (f"{pose}.jpg", data, "image/jpeg") for pose, data in images.items()}
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(self.url, files=files, data={"age": str(age or "")})
                r.raise_for_status()
                d = r.json()
                return EngineResult(d.get("engine", "http"), d["diagnosis"], d.get("anti_aging", {}),
                                    float(d.get("confidence", 1.0)), d.get("flags", []))

        return await with_retries(call, "analysis_engine.analyse")


class SkinModelEngine:
    """The clinic's skin diagnosis model for what it covers, the estimate for the rest."""

    name = "skin-model-v1"

    def __init__(self, fallback: AnalysisEngine | None = None):
        self.fallback = fallback or MockAnalysisEngine()

    async def analyse(self, images: dict[str, bytes], age: int | None) -> EngineResult:
        from app.integrations import skin_models as sm

        base = await self.fallback.analyse(images, age)
        for v in base.diagnosis.values():
            v["source"] = "estimate"
        svc = sm.diagnosis_service()
        if svc is None:
            base.model = {"status": "off", "message": "No skin diagnosis model is configured; all scores are estimates."}
            return base

        per_pose: dict[str, dict | None] = {}
        out_images: list[EngineImage] = []
        problem = None
        for pose in ("front", "left", "right"):
            if pose not in images:
                continue
            try:
                reply = await sm.call_service(svc, images[pose])
            except sm.ModelUnavailable as e:
                problem = str(e)
                break  # the service is down: don't wait on it for the other photos
            per_pose[pose] = reply.details
            out_images.append(EngineImage("diagnosis_overlay", pose, reply.image, reply.content_type, svc.name,
                                          {**(reply.details or {}), "seconds": reply.seconds}))

        if not out_images:
            base.model = {"status": "unavailable", "message": f"{problem} All scores are estimates."}
            return base
        base.images = out_images
        poses = {p: ({"face_detected": d.get("face_detected"), "found": _counts(d)} if d else None)
                 for p, d in per_pose.items()}
        if all(d is None for d in per_pose.values()):
            base.engine = "skin-model-images+mock"
            base.model = {"status": "no_findings", "poses": poses,
                          "message": "The model sent marked-up photos but not its findings (it needs the update that "
                                     "adds return_predictions), so the scores are still estimates."}
            return base

        summary = sm.summarise({p: d for p, d in per_pose.items() if d}, svc.classes())
        if not summary["poses_used"]:
            base.engine = "skin-model-images+mock"
            base.confidence = round(max(0.0, base.confidence - 0.25), 2)
            base.model = {"status": "no_face", "poses": poses,
                          "message": "The model did not find a face in the photos, so the scores are estimates. "
                                     "Retake the photos facing the camera in even light."}
            return base
        for concern, v in summary["scores"].items():
            base.diagnosis[concern] = {"score": v["score"], "label": severity_label(v["score"]), "source": "model",
                                       "count": v["count"], "area_pct": v["area_pct"], "pose": v["pose"]}
        base.engine = self.name
        base.model = {"status": "ok" if problem is None else "partial", "poses": poses, "extras": summary["extras"],
                      "classes": svc.classes(),
                      "message": problem and f"Only some photos were analysed: {problem}"}
        if "front" not in summary["poses_used"]:
            base.confidence = round(max(0.0, base.confidence - 0.15), 2)
        return base


def _counts(d: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in d.get("detections") or []:
        out[x.get("class", "?")] = out.get(x.get("class", "?"), 0) + 1
    return out


_engine: AnalysisEngine | None = None


def get_analysis_engine() -> AnalysisEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        if s.analysis_engine == "http" and s.analysis_engine_url:
            _engine = HttpAnalysisEngine(s.analysis_engine_url)
        elif s.analysis_engine == "ws":
            _engine = SkinModelEngine()
        else:
            _engine = MockAnalysisEngine()
    return _engine


def set_analysis_engine(engine: AnalysisEngine | None) -> None:
    """Test hook."""
    global _engine
    _engine = engine
