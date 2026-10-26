"""Know your Skin: conversational questionnaire, skip logic and skin-type derivation."""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge import questionnaire, rules
from app.models import Assessment, Client, Visit
from app.services import DomainError

NONE_WORDS = {"none", "no", "nothing", "nil", "na", "n/a", "not really", "nope", "no medicines", "no allergies"}


def applicable(q: dict, client: Client) -> bool:
    cond = q.get("applies_if")
    if not cond:
        return True
    if "gender_in" in cond:
        return client.gender in cond["gender_in"]
    return True


def applicable_questions(client: Client) -> list[dict]:
    return [q for q in questionnaire()["questions"] if applicable(q, client)]


def next_question(assessment: Assessment | None, client: Client) -> dict | None:
    answers = assessment.answers if assessment else {}
    for q in applicable_questions(client):
        if q["id"] not in answers:
            return q
    return None


def progress(assessment: Assessment | None, client: Client) -> tuple[int, int]:
    qs = applicable_questions(client)
    answers = assessment.answers if assessment else {}
    return sum(1 for q in qs if q["id"] in answers), len(qs)


def parse_answer(q: dict, text: str) -> str | None:
    """Map a free-text reply to an option value (choice) or a cleaned string (text)."""
    t = (text or "").strip()
    if not t:
        return None
    if q["type"] == "text":
        return "none" if t.lower().strip(" .!") in NONE_WORDS else t[:300]
    low = t.lower().strip(" .!")
    opts = q["options"]
    m = re.fullmatch(r"(?:option\s*)?(\d)", low)
    if m and 1 <= int(m.group(1)) <= len(opts):
        return opts[int(m.group(1)) - 1]["value"]
    for o in opts:
        if low == o["value"].lower() or low == o["label"].lower():
            return o["value"]
    # label contained in the reply, longest label first
    for o in sorted(opts, key=lambda o: -len(o["label"])):
        if o["label"].lower() in low:
            return o["value"]
    # loose keyword overlap
    best, best_score = None, 0
    words = set(re.findall(r"[a-z0-9]+", low))
    for o in opts:
        ow = set(re.findall(r"[a-z0-9]+", o["label"].lower())) - {"a", "the", "or", "on", "of", "i", "my", "and", "to"}
        score = len(words & ow)
        if score > best_score:
            best, best_score = o["value"], score
    return best if best_score >= 1 else None


def derive(answers: dict) -> tuple[str | None, bool, dict]:
    """Return (skin_type, sensitive, flags) from answers."""
    qs = {q["id"]: q for q in questionnaire()["questions"]}
    oil = combo = sens = 0
    concerns: set[str] = set()
    flags: dict = {}
    for qid, value in answers.items():
        q = qs.get(qid)
        if not q or q["type"] != "choice":
            continue
        opt = next((o for o in q["options"] if o["value"] == value), None)
        if not opt:
            continue
        oil += opt.get("oil", 0)
        combo += opt.get("combo", 0)
        sens += opt.get("sens", 0)
        if opt.get("concern"):
            concerns.add(opt["concern"])
        if opt.get("flag"):
            flags[opt["flag"]] = True

    skin_type = None
    if all(k in answers for k in ("q01", "q02", "q03", "q04")):
        if combo >= 2 and -1 <= oil <= 4:
            skin_type = "combination"
        elif oil >= 3:
            skin_type = "oily"
        elif oil <= -2:
            skin_type = "dry"
        else:
            skin_type = "normal"

    r = rules()
    meds = (answers.get("q13") or "").lower()
    med_tags = [tag for tag, words in r["medication_conflicts"].items() if meds != "none" and any(w in meds for w in words)]
    if med_tags:
        flags["medication_tags"] = med_tags
    allergies = (answers.get("q14") or "").lower()
    allergy_tags = [tag for tag, words in r["allergy_tags"].items() if allergies != "none" and any(w in allergies for w in words)]
    if allergy_tags:
        flags["allergy_tags"] = allergy_tags
    if allergies and allergies != "none":
        flags["allergy_text"] = allergies
    if concerns:
        flags["questionnaire_concerns"] = sorted(concerns)
    return skin_type, sens >= 2, flags


async def get_or_create(db: AsyncSession, visit: Visit, client: Client) -> Assessment:
    a = (await db.execute(select(Assessment).where(Assessment.visit_id == visit.id))).scalar_one_or_none()
    if a:
        return a
    a = Assessment(visit_id=visit.id, answers={}, skipped={})
    # Reuse stable answers from a recent previous assessment ("skips ones already answered").
    window = datetime.now(timezone.utc) - timedelta(days=get_settings().assessment_reuse_days)
    prev = (
        await db.execute(
            select(Assessment)
            .join(Visit, Visit.id == Assessment.visit_id)
            .where(Visit.client_id == client.id, Visit.id != visit.id, Assessment.completed.is_(True),
                   Assessment.updated_at > window)
            .order_by(Assessment.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prev:
        stable = {q["id"] for q in questionnaire()["questions"] if q.get("stable")}
        a.answers = {k: v for k, v in prev.answers.items() if k in stable}
        a.skipped = {k: "reused_from_previous_visit" for k in a.answers}
    for q in questionnaire()["questions"]:
        if not applicable(q, client):
            a.skipped[q["id"]] = "not_applicable"
    _recompute(a, client)
    db.add(a)
    await db.flush()
    return a


def _recompute(a: Assessment, client: Client) -> None:
    a.skin_type, a.sensitive, a.flags = derive(a.answers)
    a.completed = next_question(a, client) is None


async def save_answers(db: AsyncSession, visit: Visit, client: Client, answers: dict[str, str]) -> Assessment:
    qs = {q["id"]: q for q in questionnaire()["questions"]}
    a = await get_or_create(db, visit, client)
    merged = dict(a.answers)
    for qid, value in answers.items():
        q = qs.get(qid)
        if q is None:
            raise DomainError("unknown_question", f"Unknown question id {qid}.", 422)
        if not applicable(q, client):
            continue
        value = str(value).strip()
        if q["type"] == "choice" and value not in {o["value"] for o in q["options"]}:
            parsed = parse_answer(q, value)
            if parsed is None:
                raise DomainError("invalid_answer", f"'{value}' is not a valid answer for {qid}.", 422)
            value = parsed
        if q["type"] == "text":
            value = parse_answer(q, value) or "none"
        merged[qid] = value
    a.answers = merged  # reassign so JSONB change is tracked
    _recompute(a, client)
    await db.flush()
    return a
