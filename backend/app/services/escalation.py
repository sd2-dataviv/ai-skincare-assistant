"""Escalation rules — enforced in code, not left to the AI model (spec §05, §08).

`evaluate()` is pure: given the current state it returns every trigger that applies.
`check_and_escalate()` raises an Escalation for any trigger not already raised in this conversation/visit,
and switches the conversation to handed_over so the AI can no longer act.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge import rules
from app.models import Analysis, Assessment, Client, Conversation, Escalation, Prescription, Visit
from app.services.clients import age_of

TRIGGER_TEXT = {
    "under_18": "The client is under 18.",
    "pregnancy": "The client is pregnant or trying to conceive.",
    "breastfeeding": "The client is breast-feeding.",
    "medication_conflict": "The client takes medication that conflicts with treatments.",
    "non_skincare_finding": "A photo shows something outside skincare.",
    "low_confidence": "Analysis confidence is below the threshold.",
    "user_requested": "The client asked to speak to a person.",
    "doctor_rejected": "The doctor rejected the draft plan.",
    "ai_requested": "The AI requested a doctor.",
    "staff_requested": "Clinic staff requested a doctor.",
    "medical_concern": "The client described an injury or urgent medical problem.",
}


@dataclass
class Trigger:
    code: str
    details: str


_HUMAN_RE = re.compile(
    r"\b(talk|speak|chat|connect|transfer|put)\b[\w\s,]{0,20}?\b(doctor|dr|dermatologist|human|person|someone|somebody|staff|nurse)\b"
    r"|\b(need|want|get)\b[\w\s]{0,12}?\b(a|the|real)\s+(doctor|human|person)\b"
)


def detect_human_request(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").lower())
    return any(p in t for p in rules()["human_request_phrases"]) or bool(_HUMAN_RE.search(t))


_MEDICAL_RE = re.compile(
    r"\b(burn(?:s|t|ed|ing)?|sunburn(?:t|ed)?|bleed(?:s|ing)?|wound|infect(?:ion|ed)?|pus|oozing|swollen|swelling|fever"
    r"|blister(?:s|ed|ing)?|severe pain|allergic reaction|anaphyla\w*|mole (?:is )?(?:changing|changed|growing|bleeding))\b")


def detect_medical_concern(text: str, ignore: str = "") -> str | None:
    """Injury or urgent symptoms in the client's words. `ignore` holds the current question's options, so answering
    'Burns easily, rarely tans' to the sun question isn't mistaken for an injury."""
    m = _MEDICAL_RE.search((text or "").lower())
    if not m:
        return None
    if m.group(0)[:4] in (ignore or "").lower():
        return None
    return m.group(0)


def evaluate(client: Client | None, assessment: Assessment | None, analyses: list[Analysis],
             user_text: str | None = None, medical_ignore: str = "") -> list[Trigger]:
    out: list[Trigger] = []
    if user_text and detect_human_request(user_text):
        out.append(Trigger("user_requested", "Client asked: " + user_text[:200]))
    if user_text:
        word = detect_medical_concern(user_text, medical_ignore)
        if word:
            out.append(Trigger("medical_concern", f"Client mentioned '{word}': " + user_text[:200]))
    if client is not None:
        age = age_of(client)
        if age is not None and age < 18:
            out.append(Trigger("under_18", f"Age {age}."))
    if assessment is not None:
        f = assessment.flags or {}
        if f.get("pregnancy"):
            out.append(Trigger("pregnancy", "Answered pregnant or trying to conceive."))
        if f.get("breastfeeding"):
            out.append(Trigger("breastfeeding", "Answered breast-feeding."))
        conflict = [t for t in f.get("medication_tags", []) if t in rules()["escalation_medication"]]
        if conflict:
            out.append(Trigger("medication_conflict", "Medication: " + (assessment.answers.get("q13") or "")[:200]
                               + f" ({', '.join(conflict)})."))
    diag = [a for a in analyses if a.kind == "diagnosis"]
    if diag:
        last = diag[-1]
        for flag in (last.result or {}).get("flags", []):
            if flag.get("type") == "non_skincare":
                out.append(Trigger("non_skincare_finding", flag.get("note", "Possible non-skincare finding.")))
        if last.confidence < get_settings().escalation_min_confidence:
            out.append(Trigger("low_confidence", f"Confidence {last.confidence:.2f}."))
    return out


async def already_raised(db: AsyncSession, conversation: Conversation | None, visit: Visit | None) -> set[str]:
    conds = []
    if conversation is not None:
        conds.append(Escalation.conversation_id == conversation.id)
    if visit is not None:
        conds.append(Escalation.visit_id == visit.id)
    if not conds:
        return set()
    rows = (await db.execute(select(Escalation.trigger).where(or_(*conds)))).scalars().all()
    return set(rows)


async def escalate(db: AsyncSession, conversation: Conversation | None, visit: Visit | None, code: str,
                   details: str) -> Escalation:
    esc = Escalation(conversation_id=conversation.id if conversation else None,
                     visit_id=visit.id if visit else None, trigger=code, details=details[:1000], status="open")
    db.add(esc)
    if conversation is not None and conversation.mode == "ai":
        conversation.mode = "handed_over"
    if visit is not None and visit.status in ("in_progress", "awaiting_approval"):
        visit.status = "escalated"
    await db.flush()
    return esc


async def open_escalations(db: AsyncSession, conversation: Conversation | None, visit: Visit | None) -> list[Escalation]:
    conds = []
    if conversation is not None:
        conds.append(Escalation.conversation_id == conversation.id)
    if visit is not None:
        conds.append(Escalation.visit_id == visit.id)
    if not conds:
        return []
    return list((await db.execute(select(Escalation).where(Escalation.status != "resolved", or_(*conds)))).scalars())


async def check_and_escalate(db: AsyncSession, conversation: Conversation | None, visit: Visit | None,
                             client: Client | None, assessment: Assessment | None, analyses: list[Analysis],
                             user_text: str | None = None, medical_ignore: str = "") -> list[Escalation]:
    if conversation is None and visit is not None:
        conversation = (await db.execute(select(Conversation).where(Conversation.visit_id == visit.id)
                                         .order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()
    raised = await already_raised(db, conversation, visit)
    new = []
    for trig in evaluate(client, assessment, analyses, user_text, medical_ignore):
        # A client can ask for a person more than once; clinical triggers are raised once.
        if trig.code in raised and trig.code != "user_requested":
            continue
        if trig.code == "user_requested" and conversation is not None and conversation.mode != "ai":
            continue
        new.append(await escalate(db, conversation, visit, trig.code, trig.details))
        raised.add(trig.code)
    return new


async def resolve(db: AsyncSession, esc: Escalation, conversation: Conversation | None, visit: Visit | None,
                  hand_back_to_ai: bool) -> None:
    esc.status = "resolved"
    esc.resolved_at = datetime.now(timezone.utc)
    # Only match on ids that exist: `visit_id == None` would compile to IS NULL and pick up other clients' cases.
    conds = []
    if conversation is not None:
        conds.append(Escalation.conversation_id == conversation.id)
    if visit is not None:
        conds.append(Escalation.visit_id == visit.id)
    open_left = []
    if conds:
        open_left = (
            await db.execute(select(Escalation).where(Escalation.status != "resolved", Escalation.id != esc.id, or_(*conds)))
        ).scalars().all()
    if not hand_back_to_ai and conversation is not None and visit is None:
        conversation.mode = "closed"  # nothing left for anyone to act on
    if hand_back_to_ai and not open_left:
        if conversation is not None and conversation.mode == "handed_over":
            conversation.mode = "ai"
        if visit is not None and visit.status == "escalated":
            rx = (await db.execute(select(Prescription).where(Prescription.visit_id == visit.id))).scalar_one_or_none()
            visit.status = {"pending_approval": "awaiting_approval", "approved": "approved"}.get(
                rx.status if rx else "", "in_progress")
    await db.flush()
