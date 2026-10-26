"""Test harness: real PostgreSQL (skincare_test), scripted agent, mock adapters, face check off."""
import os
import tempfile
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://skincare:skincare@127.0.0.1:5433/skincare_test")
os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_PROVIDER"] = "scripted"
os.environ["PHOTO_FACE_CHECK"] = "false"
os.environ["OTP_MODE"] = "mock"
os.environ["JWT_SECRET"] = "test-secret-0123456789abcdef-0123456789"
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="skincare-photos-")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, get_engine, sessionmaker  # noqa: E402
from app.integrations.analysis_engine import set_analysis_engine  # noqa: E402
from app.integrations.messaging import OUTBOX  # noqa: E402
from app.integrations.platform_auth import gateway_tokens  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_catalogue, seed_users  # noqa: E402
from app.services.ot import seed_doctor_hours, seed_equipment, seed_rooms  # noqa: E402

PASSWORD = "Demo@12345"
KEEP = {"users", "catalogue_items", "ot_rooms", "ot_equipment", "doctor_hours"}


@pytest.fixture(scope="session", autouse=True)
async def database():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with sessionmaker()() as db:
        await seed_catalogue(db)
        await seed_users(db, PASSWORD)
        await seed_rooms(db)
        await seed_equipment(db)
        await db.flush()
        await seed_doctor_hours(db)
        await db.commit()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables():
    tables = [t.name for t in Base.metadata.sorted_tables if t.name not in KEEP]
    async with get_engine().begin() as conn:
        await conn.execute(text("TRUNCATE " + ", ".join(tables) + " CASCADE"))
    OUTBOX.sent.clear()
    set_analysis_engine(None)
    gateway_tokens.reset()  # the model gateway's platform session is process-wide
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client: AsyncClient, email: str) -> dict:
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def operator(client):
    return await login(client, "operator@clinic.local")


@pytest.fixture
async def doctor(client):
    return await login(client, "doctor@clinic.local")


@pytest.fixture
async def admin(client):
    return await login(client, "admin@clinic.local")


def face_like_jpeg(seed: int = 1, brightness: int = 150, blur: bool = False, size: int = 800) -> bytes:
    """A skin-toned, textured test image (face detection is disabled in tests)."""
    rng = np.random.default_rng(seed)
    img = np.zeros((size, size, 3), np.uint8)
    img[:] = tuple(int(np.clip(v, 0, 255)) for v in (brightness - 40, brightness - 10, brightness + 20))  # BGR skin tone
    noise = rng.normal(0, 18, (size, size, 3))
    img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    cv2.ellipse(img, (size // 2, size // 2), (size // 4, size // 3), 0, 0, 360,
                tuple(int(np.clip(v, 0, 255)) for v in (brightness - 30, brightness, brightness + 30)), -1)
    for _ in range(40):
        x, y = rng.integers(size // 4, size - size // 4, 2)
        cv2.circle(img, (int(x), int(y)), int(rng.integers(3, 9)), (90, 80, 140), -1)
    if blur:
        img = cv2.GaussianBlur(img, (51, 51), 25)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    assert ok
    return buf.tobytes()


class Session:
    """Drives a consultation through the public API like the React app does."""

    def __init__(self, client: AsyncClient, headers: dict):
        self.c = client
        self.h = headers
        self.id: str | None = None
        self.ui: dict = {}
        self.transcript: list[dict] = []

    async def start(self) -> dict:
        r = await self.c.post("/api/conversations", json={"consent_ai": True, "consent_recording": True}, headers=self.h)
        assert r.status_code == 201, r.text
        data = r.json()
        self.id = data["conversation_id"]
        self.ui = data["ui"]
        self.transcript += data["messages"]
        return data

    async def say(self, text: str, cmid: str | None = None) -> dict:
        r = await self.c.post(f"/api/conversations/{self.id}/messages",
                              json={"text": text, "client_message_id": cmid or uuid.uuid4().hex}, headers=self.h)
        assert r.status_code == 200, r.text
        data = r.json()
        self.ui = data["ui"]
        self.transcript += data["messages"]
        return data

    async def photo(self, pose: str, data: bytes) -> dict:
        r = await self.c.post(f"/api/conversations/{self.id}/photos", data={"pose": pose},
                              files={"file": (f"{pose}.jpg", data, "image/jpeg")}, headers=self.h)
        assert r.status_code == 200, r.text
        out = r.json()
        self.ui = out["ui"]
        self.transcript += out["messages"]
        return out

    def last(self) -> str:
        return self.transcript[-1]["content"] if self.transcript else ""

    async def identify(self, mobile: str = "98765 43210") -> None:
        await self.say(f"Hi, my number is {mobile}")
        assert self.ui["input"] == "otp", self.last()
        await self.say("123456")

    async def answer_all(self, overrides: dict[str, str] | None = None) -> None:
        overrides = overrides or {}
        for _ in range(25):
            if self.ui["stage"] != "assess":
                return
            q = self.ui["question"]
            answer = overrides.get(q["id"]) or ("none" if q["type"] == "text" else self.ui["quick_replies"][0])
            if q["id"] == "q15" and "q15" not in overrides:
                answer = "No"
            await self.say(answer)
        raise AssertionError("questionnaire did not finish")

    async def refresh(self) -> dict:
        r = await self.c.get(f"/api/conversations/{self.id}", headers=self.h)
        assert r.status_code == 200, r.text
        data = r.json()
        self.ui = data["ui"]
        return data

    async def book_all(self, choice: int = 0) -> None:
        """Tap an offered treatment-room slot for every procedure until the visit wraps up."""
        await self.refresh()
        for _ in range(8):
            if self.ui["stage"] != "booking":
                return
            await self.say(self.ui["quick_replies"][choice])
        raise AssertionError("booking did not finish")

    async def photos(self, seed: int = 1) -> dict:
        out = {}
        for i, pose in enumerate(["front", "left", "right"]):
            out = await self.photo(pose, face_like_jpeg(seed + i))
        return out
