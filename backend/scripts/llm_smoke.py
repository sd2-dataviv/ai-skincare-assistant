"""Live end-to-end check of the configured LLM (e.g. Groq gpt-oss-120b) with a brand-new client.

New client → OTP → profile → concern → all 18 questions (mix of free text and taps) → 3 uploaded photos →
analysis + draft → doctor approval → wrap-up. Prints every AI reply so you can read the conversation.

Runs in-process against the TEST database (skincare_test) so it never touches clinic data.
Usage (from backend/):  .venv/bin/python -m scripts.llm_smoke
"""
import asyncio
import os
import time
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://skincare:skincare@127.0.0.1:5433/skincare_test")
os.environ["PHOTO_FACE_CHECK"] = "false"  # synthetic photos have no face

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, get_engine, sessionmaker  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AIAction  # noqa: E402
from app.seed import seed_catalogue, seed_users  # noqa: E402

# Free-text answers exercise the LLM; anything not listed is answered by tapping the first option.
FREE_TEXT = {
    "q01": "it gets greasy in the middle of my face by lunch",
    "q03": "sorry",                                   # an apology / filler
    "q05": "what does reacting mean exactly?",        # a side question first
    "q09": "hmm what was it I told you my problem was?",  # memory
    "q13": "nothing, no medicines",
    "q14": "no allergies that I know of",
    "q15": "no",
}


def photo(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    img = np.clip(np.full((900, 900, 3), (115, 145, 185), np.float32) + rng.normal(0, 16, (900, 900, 3)), 0, 255).astype(np.uint8)
    cv2.ellipse(img, (450, 450), (230, 300), 0, 0, 360, (125, 160, 200), -1)
    return cv2.imencode(".jpg", img)[1].tobytes()


class Run:
    def __init__(self, c: AsyncClient, h: dict):
        self.c, self.h, self.n, self.ui, self.cid = c, h, 0, {}, None

    def show(self, who: str, text: str, extra: str = "") -> None:
        print(f"\n[{self.n:02d}] {who}: {text}{extra}")

    async def refresh(self) -> None:
        r = await self.c.get(f"/api/conversations/{self.cid}", headers=self.h)
        self.ui = r.json()["ui"]

    async def say(self, msg: str) -> None:
        self.n += 1
        t0 = time.perf_counter()
        r = await self.c.post(f"/api/conversations/{self.cid}/messages",
                              json={"text": msg, "client_message_id": uuid.uuid4().hex}, headers=self.h)
        r.raise_for_status()
        d = r.json()
        self.ui = d["ui"]
        self.show("USER", msg, f"   ({time.perf_counter() - t0:.1f}s, stage={self.ui['stage']})")
        for m in d["messages"]:
            print(f"     AI ({m['meta'].get('agent')}): {m['content']}")

    async def upload(self, pose: str, data: bytes) -> None:
        self.n += 1
        t0 = time.perf_counter()
        r = await self.c.post(f"/api/conversations/{self.cid}/photos", data={"pose": pose},
                              files={"file": (f"{pose}.jpg", data, "image/jpeg")}, headers=self.h)
        r.raise_for_status()
        d = r.json()
        self.ui = d["ui"]
        self.show("PHOTO", pose, f"   ({time.perf_counter() - t0:.1f}s, stage={self.ui['stage']})")
        for m in d["messages"]:
            print(f"     AI ({m['meta'].get('agent')}): {m['content']}")


async def main() -> None:
    setup_logging()
    s = get_settings()
    name, _, _, model = s.resolved_llm
    print(f"[smoke] provider={name} model={model} reasoning_effort={s.llm_reasoning_effort}")
    if not s.database_url.rsplit("/", 1)[-1].endswith("_test"):
        raise SystemExit("[smoke] Refusing to run: DATABASE_URL must point at a *_test database.")
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables if t.name not in ("users", "catalogue_items"))
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))  # fresh run (test DB only)
    async with sessionmaker()() as db:
        await seed_catalogue(db)
        await seed_users(db, "Demo@12345")
        from app.services.ot import seed_rooms

        await seed_rooms(db)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=180) as c:
        async def login(email):
            r = await c.post("/api/auth/login", json={"email": email, "password": "Demo@12345"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        op, dr = await login("operator@clinic.local"), await login("doctor@clinic.local")
        conv = (await c.post("/api/conversations", json={"consent_ai": True, "consent_recording": True}, headers=op)).json()
        run = Run(c, op)
        run.cid = conv["conversation_id"]
        run.show("AI", conv["messages"][0]["content"])

        for msg in ["Hi, my number is 91234 56780", "123456", "Meera Iyer", "14-02-1995", "Female",
                    "hi! honestly my chin keeps breaking out before exams and I've got these dark patches on my cheeks"]:
            await run.say(msg)

        asked: set[str] = set()
        for _ in range(30):
            if run.ui["stage"] != "assess":
                break
            q = run.ui["question"]
            if q["id"] in asked:
                answer = run.ui["quick_replies"][1 if len(run.ui["quick_replies"]) > 1 else 0]
            else:
                answer = FREE_TEXT.get(q["id"]) or run.ui["quick_replies"][0]
                if q["id"] in ("q03", "q05", "q09"):
                    asked.add(q["id"])
            await run.say(answer)

        if run.ui["stage"] == "capture":
            await run.say("done")  # must NOT be treated as a photo
            for i, pose in enumerate(["front", "left", "right"]):
                await run.upload(pose, photo(i + 1))

        visit_id = run.ui.get("visit_id")
        if run.ui["stage"] == "approval":
            await run.say("what does the laser treatment involve?")
            r = await c.post(f"/api/visits/{visit_id}/approval/decision", json={"decision": "approve"}, headers=dr)
            run.n += 1
            run.show("DOCTOR", f"approved (HTTP {r.status_code})")
            for m in r.json().get("conversation_messages", []):
                print(f"     AI ({m['meta'].get('agent')}): {m['content']}")
            await run.refresh()

        if run.ui["stage"] == "booking":  # treatment-room booking in free text
            for msg in ["how long does it take, and do I need to prepare anything?",
                        "do you have anything next wednesday afternoon?", "the second one"]:
                await run.say(msg)
            for _ in range(4):
                if run.ui["stage"] != "booking":
                    break
                await run.say(run.ui["quick_replies"][0])

        # Second client: an injury should go straight to a doctor
        print("\n[smoke] ---- second client: injury ----")
        conv2 = (await c.post("/api/conversations", json={"consent_ai": True, "consent_recording": True}, headers=op)).json()
        run2 = Run(c, op)
        run2.cid = conv2["conversation_id"]
        for msg in ["98111 22233", "123456", "I burnt my hand on the stove yesterday and it's blistering"]:
            await run2.say(msg)
        print(f"[smoke] second client mode: {run2.ui['mode']}")

    async with sessionmaker()() as db:
        acts = (await db.execute(select(AIAction).where(AIAction.conversation_id == uuid.UUID(run.cid))
                                 .order_by(AIAction.created_at))).scalars().all()
        bad = [a for a in acts if a.status != "ok"]
        print(f"\n[smoke] {len(acts)} actions, {len(bad)} not ok:")
        for a in bad:
            print(f"  {a.action:<20} {a.status:<8} {a.actor:<10} {(a.result or {}).get('error', '')[:80]}")
        print(f"[smoke] final stage: {run.ui['stage']}")


if __name__ == "__main__":
    asyncio.run(main())
