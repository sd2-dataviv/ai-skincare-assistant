"""Skin analysis engine adapter (diagnosis + anti-ageing).

`MockAnalysisEngine` derives plausible, deterministic scores from simple image statistics so the full flow
can be exercised end-to-end. It is NOT a medical model; results are labelled engine="mock".
`HttpAnalysisEngine` posts the photos to the clinic's existing analysis service.
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
class EngineResult:
    engine: str
    diagnosis: dict[str, dict]  # concern -> {score, label}
    anti_aging: dict
    confidence: float
    flags: list[dict] = field(default_factory=list)  # [{type: "non_skincare", note: "..."}]


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


_engine: AnalysisEngine | None = None


def get_analysis_engine() -> AnalysisEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = HttpAnalysisEngine(s.analysis_engine_url) if s.analysis_engine == "http" and s.analysis_engine_url else MockAnalysisEngine()
    return _engine


def set_analysis_engine(engine: AnalysisEngine | None) -> None:
    """Test hook."""
    global _engine
    _engine = engine
