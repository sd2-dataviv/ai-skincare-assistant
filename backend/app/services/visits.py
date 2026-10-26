import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import rules
from app.models import Client, Visit

PROGRAMME_PRIORITY = ["acne", "pigmentation", "anti_ageing", "glow_hydration"]


def detect_concerns(text: str) -> list[str]:
    t = (text or "").lower()
    found = []
    for concern, words in rules()["concern_keywords"].items():
        if any(w in t for w in words):
            found.append(concern)
    return found


def programme_for(concerns: list[str]) -> str:
    progs = rules()["programmes"]
    for p in PROGRAMME_PRIORITY:
        if any(c in progs[p]["concerns"] for c in concerns):
            return p
    return "general"


async def last_visit(db: AsyncSession, client_id: uuid.UUID, exclude: uuid.UUID | None = None) -> Visit | None:
    q = select(Visit).where(Visit.client_id == client_id)
    if exclude:
        q = q.where(Visit.id != exclude)
    return (await db.execute(q.order_by(Visit.created_at.desc()).limit(1))).scalar_one_or_none()


async def start_visit(db: AsyncSession, client: Client, concern_text: str, user_id: uuid.UUID | None,
                      summary: str | None = None) -> Visit:
    """Pick the programme from the concern (and the AI's one-line summary of it) and history; set the session number."""
    concerns = detect_concerns(f"{concern_text} {summary or ''}")
    previous = await last_visit(db, client.id)
    programme = programme_for(concerns)
    if previous and (programme == "general" or previous.programme == programme):
        # Continuing the same programme (or no clear new concern): stay in it.
        programme = previous.programme
        if not concerns:
            concerns = list(previous.concerns or [])
    count = await db.scalar(
        select(func.count()).select_from(Visit).where(Visit.client_id == client.id, Visit.programme == programme)
    )
    visit = Visit(
        client_id=client.id, programme=programme, session_number=(count or 0) + 1,
        concern_text=(concern_text or "").strip()[:2000], concerns=concerns, created_by_user_id=user_id,
    )
    db.add(visit)
    await db.flush()
    return visit


def programme_label(programme: str) -> str:
    return rules()["programmes"].get(programme, {}).get("label", programme)
