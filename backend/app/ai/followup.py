"""Follow-up assistant: answers a client's questions about a past consultation from its approved plan only.

Safety is decided in code before the model is asked: a request for a person or a worrying symptom goes straight to the
clinic team. The model may also hand over; it can never change a plan or a booking, and sentences that claim it did
are dropped. Without an LLM key (or when the provider fails) a small scripted assistant answers instead.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agents import clean_reply, parse_decision
from app.ai.providers import LLMError, get_provider
from app.models import Client, Thread, ThreadMessage
from app.services.escalation import detect_human_request, detect_medical_concern
from app.services.history import client_history

log = logging.getLogger(__name__)


@dataclass
class Answer:
    replies: list[str] = field(default_factory=list)
    handover: tuple[str, str] | None = None
    agent: str = "scripted"


PERSON_REPLY = ("I've asked the clinic team to reply here. A doctor or a member of the team will answer as soon as they "
                "can, and we'll text you when they do.")
MEDICAL_REPLY = ("I'm sorry you're dealing with that. I've flagged it to a doctor, who will reply here. If it's severe or "
                 "getting worse quickly, please call the clinic or get urgent medical help now.")
CHANGE_REPLY = ("I can't change your plan or appointments from here, so I've passed this to the clinic team. They'll "
                "reply here, and we'll text you when they do.")
UNANSWERED_REPLY = ("That's a good question for the clinic team, so I've passed it on. They'll reply here, and we'll "
                    "text you when they do.")
NO_PLAN_REPLY = ("This consultation doesn't have a doctor-approved plan, so I can't go through its details. I've asked "
                 "the clinic team to reply here.")

_THANKS = re.compile(r"^\W*(thanks|thank you|thank u|thx|ty|ok|okay|great|cool|fine|got it|noted|perfect|alright)\b", re.I)
_GREET = re.compile(r"^\W*(hi|hello|hey|hii+|namaste|good (morning|afternoon|evening))\W*$", re.I)
_CHANGE = re.compile(r"\b(reschedul\w*|cancel\w*|postpone\w*|refund\w*|discount\w*|move (my|the)|shift (my|the)|"
                     r"change (my|the)|different (time|date|day|slot)|another (time|date|day|slot)|stop (using|the)|"
                     r"switch (to|my)|increase|decrease|more often|less often|add (a|another))\b", re.I)
_WHEN = re.compile(r"\b(when|next|appointment|appointments|session|sessions|date|time|booking|booked|schedule)\b", re.I)
_PLAN = re.compile(r"\b(plan|product|products|cream|serum|gel|wash|cleanser|sunscreen|spf|apply|use|using|routine|"
                   r"how often|how many|treatment|treatments|instructions|morning|night|prescri\w*)\b", re.I)
_CLAIMS = re.compile(r"\b(i(?:'ve| have)? (booked|rescheduled|cancelled|canceled|changed|updated|moved|added|removed|"
                     r"prescribed|reserved|confirmed)|has been (booked|rescheduled|cancelled|changed|updated|moved))\b", re.I)

SYSTEM = """You are the AI assistant of a dermatology clinic in India, answering a client's follow-up questions in a chat.
Use only the FACTS below: the client's own consultations, with findings and plans that a doctor approved.
Rules:
- Short, warm, plain language; British spelling; at most 90 words. No markdown headings.
- You are not a doctor. Never diagnose, never suggest new products, treatments, doses or dates, never promise results.
- You cannot change plans or appointments and must never say you booked, cancelled, moved or changed anything.
- If the question needs a doctor's judgement, a change to the plan or appointments, mentions a symptom or reaction,
  or the FACTS don't answer it, set "handover": true and say you've passed it to the clinic team, who will reply here.
- If a consultation shows approved: false, its details are not available to you; say so.
Reply only with JSON: {"reply": "<message to the client>", "handover": false, "reason": "<why, if handing over>"}"""


async def facts_for(db: AsyncSession, t: Thread, client: Client) -> dict:
    """What the assistant may talk about: approved findings/plans only (client_history enforces NFR-03)."""
    h = await client_history(db, client.id)
    visits = h["visits"]
    if t.visit_id is not None:
        visits = [v for v in visits if v["visit_id"] == str(t.visit_id)]
    return {"client_first_name": client.full_name.split()[0], "today": date.today().isoformat(),
            "about": "one consultation" if t.visit_id else "all consultations", "consultations": visits,
            "upcoming_treatments": h["upcoming"]}


def _plan_text(v: dict) -> str:
    parts = []
    for p in v["plan"]:
        line = p["name"] + (f" ({p['sessions']} sessions)" if p.get("sessions") and p["sessions"] > 1 else "")
        if p.get("instructions"):
            line += f": {p['instructions'].rstrip('.')}"
        parts.append("• " + line)
    return "\n".join(parts)


def _when_text(f: dict) -> str | None:
    up = f["upcoming_treatments"]
    if up:
        lines = [f"• {u['label']}: {u['procedure']}" + (f", session {u['session_index']} of {u['sessions_total']}"
                                                       if u["sessions_total"] > 1 else "") + f" ({u['room_name']})"
                 + (", to be confirmed by the clinic" if u.get("tentative") else "") for u in up[:4]]
        more = f"\n…and {len(up) - 4} more." if len(up) > 4 else ""
        return "Your upcoming treatments:\n" + "\n".join(lines) + more
    nxt = [v["next_session_date"] for v in f["consultations"] if v.get("next_session_date")]
    if nxt:
        d = date.fromisoformat(max(nxt))
        return f"Your next review is planned for {d.strftime('%a %d %b %Y').replace(' 0', ' ')}. The clinic will confirm the time."
    return None


def scripted(f: dict, text: str) -> Answer:
    approved = [v for v in f["consultations"] if v["approved"]]
    t = text.strip()
    if _CHANGE.search(t):
        return Answer([CHANGE_REPLY], ("out_of_scope", "Client asked: " + t[:300]))
    if _GREET.match(t):
        return Answer([f"Hello {f['client_first_name']}! Ask me anything about your plan or appointments, or ask for "
                       "the clinic team."])
    if _THANKS.match(t) and len(t.split()) <= 5:
        return Answer(["You're welcome! I'm here if anything else comes up."])
    if _WHEN.search(t):
        w = _when_text(f)
        if w:
            return Answer([w])
    if _PLAN.search(t):
        if not approved:
            return Answer([NO_PLAN_REPLY], ("unanswered", "No approved plan. Client asked: " + t[:300]))
        v = approved[0]
        when = f" from {date.fromisoformat(v['date']).strftime('%d %b %Y').lstrip('0')}" if f["about"] != "one consultation" else ""
        return Answer([f"Here's your doctor-approved plan{when}:\n{_plan_text(v)}\n\nIf anything feels uncomfortable, "
                       "stop and tell us here."])
    return Answer([UNANSWERED_REPLY], ("unanswered", "Client asked: " + t[:300]))


def _drop_claims(reply: str) -> str:
    kept = [s for s in re.split(r"(?<=[.!?])\s+", reply.strip()) if not _CLAIMS.search(s)]
    return " ".join(kept).strip()


async def _history(db: AsyncSession, t: Thread, limit: int = 16) -> list[dict]:
    rows = list((await db.execute(select(ThreadMessage).where(ThreadMessage.thread_id == t.id)
                                  .order_by(ThreadMessage.seq.desc()).limit(limit))).scalars())[::-1]
    out = []
    for m in rows:
        if m.role == "client":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            out.append({"role": "assistant", "content": m.content})
        elif m.role == "staff":
            out.append({"role": "assistant", "content": f"[{(m.meta or {}).get('author', 'Clinic team')}] {m.content}"})
    return out


async def answer(db: AsyncSession, t: Thread, client: Client, text: str) -> Answer:
    # Code first: these never depend on the model.
    if detect_human_request(text):
        return Answer([PERSON_REPLY], ("user_requested", "Client asked: " + text[:300]))
    word = detect_medical_concern(text)
    if word:
        return Answer([MEDICAL_REPLY], ("medical_concern", f"Client mentioned '{word}': " + text[:300]))

    f = await facts_for(db, t, client)
    provider = get_provider()
    if provider is None:
        return scripted(f, text)
    try:
        msgs = [{"role": "system", "content": SYSTEM + "\n\nFACTS:\n" + json.dumps(f, default=str)}]
        msgs += await _history(db, t)
        raw = (await provider.chat(msgs, temperature=0.2)).get("content")
    except LLMError:
        log.warning("follow-up LLM unavailable; scripted answer")
        return scripted(f, text)
    d = parse_decision(raw)
    reply = clean_reply(_drop_claims(str(d.get("reply") or "")))
    if d.get("handover") is True:
        return Answer([reply or UNANSWERED_REPLY], ("ai_requested", (str(d.get("reason") or "")[:200] + " | Client asked: "
                                                                    + text[:300]).strip(" |")), agent="llm")
    if not reply:
        return scripted(f, text)
    return Answer([reply], agent="llm")
