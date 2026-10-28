import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import rules
from app.models import Client, Visit

def detect_concerns(text: str) -> list[str]:
    t = (text or "").lower()
    found = []
    for concern, words in rules()["concern_keywords"].items():
        if any(w in t for w in words):
            found.append(concern)
    return found


def canonical(programme: str) -> str:
    """Today's key for a programme (visits filed before the services were renamed keep their old key)."""
    return rules().get("legacy_programmes", {}).get(programme, programme)


def _keys_for(programme: str) -> list[str]:
    return [programme] + [old for old, new in rules().get("legacy_programmes", {}).items() if new == programme]


def programme_for(concerns: list[str], text: str = "") -> str | None:
    """The service a consultation is filed under: a doctor-led one when the client's words name it (Medical
    Consultation, Anti Aging), otherwise the Scription for their main skin concern. None when nothing is clear."""
    r = rules()
    progs = r["programmes"]
    t = (text or "").lower()
    for key in r["programme_priority"]:
        prog = progs[key]
        if prog.get("keywords"):
            # Word starts only, so "itch" finds "itchy" but not "switched", and "rash" not "crash".
            if any(re.search(r"\b" + re.escape(k), t) for k in prog["keywords"]):
                return key
        elif any(c in prog["concerns"] for c in concerns):
            return key
    return None


async def last_visit(db: AsyncSession, client_id: uuid.UUID, exclude: uuid.UUID | None = None) -> Visit | None:
    q = select(Visit).where(Visit.client_id == client_id)
    if exclude:
        q = q.where(Visit.id != exclude)
    return (await db.execute(q.order_by(Visit.created_at.desc()).limit(1))).scalar_one_or_none()


async def start_visit(db: AsyncSession, client: Client, concern_text: str, user_id: uuid.UUID | None,
                      summary: str | None = None, service: str | None = None) -> Visit:
    """File the visit under a service (rules.json `programmes`) from the concern, the AI's one-line summary of it and
    the client's history, and number the session. Session 2 onwards of the same service is its follow-up.
    `service` files it under that service whatever was said (a medical problem handed straight to a doctor)."""
    text = f"{concern_text} {summary or ''}"
    concerns = detect_concerns(text)
    previous = await last_visit(db, client.id)
    programme = service if service in rules()["programmes"] else programme_for(concerns, text)
    if previous and (programme is None or canonical(previous.programme) == programme):
        # Continuing the same service (or no clear new concern): a follow-up of it.
        programme = canonical(previous.programme)
        if not concerns:
            concerns = list(previous.concerns or [])
    programme = programme or rules()["default_programme"]
    if not concerns and programme == "anti_aging":
        concerns = ["wrinkles"]  # "I'd like Botox": the plan still looks at lines and wrinkles
    count = await db.scalar(
        select(func.count()).select_from(Visit).where(Visit.client_id == client.id,
                                                      Visit.programme.in_(_keys_for(programme)))
    )
    visit = Visit(
        client_id=client.id, programme=programme, session_number=(count or 0) + 1,
        concern_text=(concern_text or "").strip()[:2000], concerns=concerns, created_by_user_id=user_id,
    )
    db.add(visit)
    await db.flush()
    return visit


def programme_label(programme: str, session_number: int | None = None) -> str:
    """The service's name as the clinic uses it. From session 2 it is a follow-up: "Medical Follow Up" after a Medical
    Consultation, otherwise "Follow Up (<service>)"."""
    r = rules()
    prog = r["programmes"].get(canonical(programme))
    if prog is None:
        return programme
    if session_number and session_number > 1:
        return prog.get("follow_up_label") or f"{r['follow_up_label']} ({prog['label']})"
    return prog["label"]


def services() -> list[dict]:
    """The clinic's consultation services, for staff screens and the API."""
    r = rules()
    out = []
    for key, prog in r["programmes"].items():
        out.append({"key": key, "label": prog["label"], "doctor_led": bool(prog.get("doctor_led")),
                    "follow_up": prog.get("follow_up_label") or f"{r['follow_up_label']} ({prog['label']})"})
    return out
