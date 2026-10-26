"""Versioned clinic knowledge: questionnaire, catalogue seed and rules."""
import json
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache
def questionnaire() -> dict:
    return json.loads((_DIR / "questionnaire.json").read_text())


@lru_cache
def catalogue_seed() -> dict:
    return json.loads((_DIR / "catalogue.json").read_text())


@lru_cache
def rules() -> dict:
    return json.loads((_DIR / "rules.json").read_text())


def question(qid: str) -> dict | None:
    return next((q for q in questionnaire()["questions"] if q["id"] == qid), None)
