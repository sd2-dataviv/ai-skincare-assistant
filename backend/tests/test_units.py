"""Pure-function tests: parsing, skin type, safety, quality gates, redaction."""
import logging

import pytest

from app.knowledge import question
from app.logging_setup import RedactingFilter, redact
from app.services import DomainError
from app.services.analysis import compare
from app.services.assessment import derive, parse_answer
from app.services.clients import extract_mobile, normalise_mobile
from app.services.escalation import detect_human_request
from app.services.photos import quality_check
from app.services.visits import detect_concerns, programme_for
from tests.conftest import face_like_jpeg


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "+919876543210"), ("+91 98765 43210", "+919876543210"), ("09876543210", "+919876543210"),
    ("91-98765-43210", "+919876543210"),
])
def test_normalise_mobile(raw, expected):
    assert normalise_mobile(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "5876543210", "abc"])
def test_normalise_mobile_rejects(raw):
    with pytest.raises(DomainError):
        normalise_mobile(raw)


def test_extract_mobile_from_sentence():
    assert extract_mobile("sure, it's 98111 22233 thanks") == "+919811122233"
    assert extract_mobile("my code is 123456") is None


def test_parse_answer_variants():
    q = question("q02")
    assert parse_answer(q, "2") == "tzone"
    assert parse_answer(q, "very shiny all over") == "very"
    assert parse_answer(q, "a little on my t-zone") == "tzone"
    assert parse_answer(q, "purple") is None
    assert parse_answer(question("q13"), "None.") == "none"


def test_skin_type_derivation():
    oily = {"q01": "shiny_all", "q02": "very", "q03": "all", "q04": "never"}
    assert derive(oily)[0] == "oily"
    dry = {"q01": "tight", "q02": "never", "q03": "barely", "q04": "often"}
    assert derive(dry)[0] == "dry"
    combo = {"q01": "tzone_shiny", "q02": "tzone", "q03": "nose", "q04": "never"}
    assert derive(combo)[0] == "combination"
    _, sensitive, flags = derive({"q05": "often", "q13": "Doxycycline for 2 weeks", "q15": "pregnant"})
    assert sensitive and flags["pregnancy"] and flags["medication_tags"] == ["photosensitising"]


def test_concern_detection_and_programme():
    assert set(detect_concerns("dark spots and pimples")) == {"pigmentation", "acne"}
    assert programme_for(["pigmentation"]) == "pigmentation"
    assert programme_for([]) is None


def test_human_request_detection():
    assert detect_human_request("Can I speak to a doctor?")
    assert not detect_human_request("my skin feels tight")


def test_quality_gates():
    assert quality_check(face_like_jpeg(1), "front")["ok"]
    dark = quality_check(face_like_jpeg(1, brightness=35), "front")
    assert not dark["ok"] and dark["issues"][0] == "too_dark"
    blurry = quality_check(face_like_jpeg(1, blur=True), "front")
    assert "blurry" in blurry["issues"]
    small = quality_check(face_like_jpeg(1, size=300), "front")
    assert "too_small" in small["issues"]
    assert quality_check(b"not an image", "front")["issues"] == ["unreadable"]


def test_compare_direction():
    before = {"acne": {"score": 60}, "pigmentation": {"score": 50}}
    after = {"acne": {"score": 42}, "pigmentation": {"score": 51}}
    out = compare(before, after, ["acne", "pigmentation"])
    assert out["acne"]["direction"] == "better" and out["acne"]["improvement_pct"] == 30.0
    assert out["pigmentation"]["direction"] == "stable"


def test_log_redaction():
    assert "98765" not in redact("OTP for +91 98765 43210 is 123456, email a@b.com")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "client %s", ("9876543210",), None)
    RedactingFilter().filter(rec)
    assert "9876543210" not in rec.getMessage()


def test_face_detector_loads_and_runs(monkeypatch):
    """The real face check path (off in the other tests): the detector must load and must not crash."""
    from app.config import get_settings
    from app.services.photos import face_check_available

    assert face_check_available(), "OpenCV build has no CascadeClassifier — install opencv-python-headless<5"
    monkeypatch.setattr(get_settings(), "photo_face_check", True)
    q = quality_check(face_like_jpeg(1), "front")
    assert "no_face" in q["issues"]  # synthetic image: no face, but no exception either
    q = quality_check(face_like_jpeg(1), "left")
    assert "no_face" in q["issues"]


@pytest.mark.parametrize("text", ["talk me to doctor", "can I speak with the dermatologist", "connect me to someone",
                                  "I want a real person", "put me through to a doctor"])
def test_human_request_variants(text):
    assert detect_human_request(text)


@pytest.mark.parametrize("text", ["my doctor said to use sunscreen", "I talk a lot on the phone", "no"])
def test_human_request_negatives(text):
    assert not detect_human_request(text)
