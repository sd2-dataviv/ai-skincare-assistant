"""AI actions (spec §06) and the single ActionLayer every caller goes through (design decision D3).

ActionLayer.call():
  1. loads state and derives the stage,
  2. blocks tools not allowed in that stage (D2) or while a doctor has the case,
  3. runs the domain service,
  4. writes an ai_actions row with inputs, result and reason (NFR-02),
  5. re-checks escalation rules in code (NFR-04) and commits.
"""
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AIAction, CatalogueItem
from app.services import DomainError
from app.services import analysis as analysis_svc
from app.services import assessment as assessment_svc
from app.services import escalation as escalation_svc
from app.services import ot as ot_svc
from app.services import otp as otp_svc
from app.services import prescriptions as rx_svc
from app.services import summary as summary_svc
from app.services.clients import age_of, find_or_import, mask_mobile, normalise_mobile
from app.services.photos import missing_poses, save_photo
from app.services.recommendations import recommend
from app.services.state import State, load_state
from app.services.visits import programme_label, start_visit

log = logging.getLogger(__name__)

ACTIVE = {"identify", "concern", "assess", "capture", "analyse", "recommend", "approval", "booking", "wrap_up"}
AFTER_CLIENT = {"concern", "assess", "capture", "analyse", "recommend", "approval", "booking", "wrap_up", "done"}

Handler = Callable[["ActionLayer", State, dict], Awaitable[dict]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    stages: set[str]
    handler: Handler
    llm: bool = True  # exposed to the LLM as a function

    def schema(self) -> dict:
        props = dict(self.parameters.get("properties", {}))
        props["reason"] = {"type": "string", "description": "Why you are taking this action (audit log)."}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": props,
                               "required": list(self.parameters.get("required", [])) + ["reason"]},
            },
        }


def _ctx(s: State) -> dict:
    return dict(s.conversation.context or {})


def _set_ctx(s: State, **updates: Any) -> None:
    ctx = _ctx(s)
    for k, v in updates.items():
        if v is None:
            ctx.pop(k, None)
        else:
            ctx[k] = v
    s.conversation.context = ctx  # reassign so the JSONB change is persisted


def _require(cond: Any, code: str, msg: str) -> None:
    if not cond:
        raise DomainError(code, msg)


# ── Handlers ──────────────────────────────────────────────────────────────────
async def h_send_otp(layer: "ActionLayer", s: State, a: dict) -> dict:
    mobile = normalise_mobile(a.get("mobile", ""))
    res = await otp_svc.send_otp(layer.db, mobile)
    _set_ctx(s, pending_mobile=mobile, awaiting="otp")
    return {**res, "to": mask_mobile(mobile)}


async def h_verify_otp(layer: "ActionLayer", s: State, a: dict) -> dict:
    mobile = _ctx(s).get("pending_mobile")
    _require(mobile, "no_pending_mobile", "Send an OTP first.")
    ok = await otp_svc.verify_otp(layer.db, mobile, str(a.get("code", "")))
    if ok:
        s.conversation.verified_mobile = mobile
        _set_ctx(s, awaiting="lookup")
    return {"verified": ok}


async def h_find_or_import_client(layer: "ActionLayer", s: State, a: dict) -> dict:
    mobile = s.conversation.verified_mobile
    _require(mobile, "not_verified", "The mobile number must be verified by OTP first.")
    dob = None
    if a.get("dob"):
        try:
            dob = date.fromisoformat(str(a["dob"]))
        except ValueError:
            raise DomainError("invalid_dob", "Date of birth must be YYYY-MM-DD.", 422)
        if dob >= date.today() or dob.year < 1900:
            raise DomainError("invalid_dob", "That date of birth isn't valid.", 422)
    gender = (a.get("gender") or None)
    client, how = await find_or_import(layer.db, mobile, a.get("full_name"), dob, gender)
    if client is None:
        _set_ctx(s, awaiting="profile_name")
        return {"status": how}
    if client.dob is None:
        # The under-18 rule needs an age: ask for the date of birth before going any further.
        _set_ctx(s, awaiting="profile_dob", profile={"existing": True})
        return {"status": "needs_dob", "first_name": client.full_name.split()[0]}
    s.conversation.client_id = client.id
    _set_ctx(s, awaiting=None, profile=None, pending_mobile=None)
    from app.services.history import count_past
    from app.services.visits import last_visit

    prev = await last_visit(layer.db, client.id)
    return {"status": how, "client_id": str(client.id), "first_name": client.full_name.split()[0],
            "age": age_of(client), "gender": client.gender, "returning": how == "found",
            "past_visits": await count_past(layer.db, client.id, None),
            "last_visit": {"date": prev.created_at.date().isoformat(), "programme": programme_label(prev.programme),
                           "session_number": prev.session_number,
                           "next_session_date": prev.next_session_date.isoformat() if prev.next_session_date else None}
            if prev else None}


async def h_start_visit(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.client, "no_client", "Identify the client first.")
    text = (a.get("concern_text") or "").strip()
    _require(text, "no_concern", "Describe the client's concern.")
    visit = await start_visit(layer.db, s.client, text, layer.user_id, summary=str(a.get("summary") or "")[:300] or None)
    s.conversation.visit_id = visit.id
    assessment = await assessment_svc.get_or_create(layer.db, visit, s.client)
    nq = assessment_svc.next_question(assessment, s.client)
    return {"visit_id": str(visit.id), "programme": visit.programme, "programme_label": programme_label(visit.programme),
            "session_number": visit.session_number, "concerns": visit.concerns,
            "reused_answers": [k for k, v in (assessment.skipped or {}).items() if v == "reused_from_previous_visit"],
            "next_question": nq}


async def h_save_assessment(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.visit and s.client, "no_visit", "Start the visit first.")
    answers = a.get("answers") or {}
    _require(isinstance(answers, dict) and answers, "no_answers", "Provide answers as {question_id: value}.")
    if layer.via_llm:
        # The model may only record the question that was just asked; it must not fill in answers ahead.
        nq = assessment_svc.next_question(s.assessment, s.client)
        current = nq["id"] if nq else None
        if set(answers) != {current}:
            raise DomainError("not_current_question", f"Only answer the current question ({current}).", 422)
    assessment = await assessment_svc.save_answers(layer.db, s.visit, s.client, answers)
    done, total = assessment_svc.progress(assessment, s.client)
    return {"saved": list(answers), "completed": assessment.completed, "skin_type": assessment.skin_type,
            "sensitive": assessment.sensitive, "progress": f"{done}/{total}",
            "next_question": assessment_svc.next_question(assessment, s.client)}


async def h_upload_photo(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.visit, "no_visit", "Start the visit first.")
    photo = await save_photo(layer.db, s.visit, a["pose"], a["data"], a["content_type"])
    s.photos.append(photo)
    return {"photo_id": str(photo.id), "pose": photo.pose, "accepted": photo.accepted,
            "issues": photo.quality.get("issues", []), "guidance": photo.quality.get("guidance"),
            "missing_poses": missing_poses(s.photos)}


async def h_run_analysis(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.visit and s.client, "no_visit", "Start the visit first.")
    res = await analysis_svc.run_analysis(layer.db, s.visit, s.client, s.photos)
    # The chat agent talks to the client: it gets the findings, not the preview (that waits for the doctor) nor the
    # model's internals (staff see those in review).
    res.pop("preview", None)
    model = res.pop("model", None) or {}
    res["scores_from"] = "skin model" if model.get("status") in ("ok", "partial") else "estimates"
    return res


async def h_get_recommendations(layer: "ActionLayer", s: State, a: dict) -> dict:
    diag = analysis_svc.latest(s.analyses, "diagnosis")
    _require(diag, "no_analysis", "Run the analysis first.")
    return await recommend(layer.db, s.visit, s.client, s.assessment, diag.result["diagnosis"])


async def h_draft_prescription(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(analysis_svc.latest(s.analyses, "diagnosis"), "no_analysis", "Run the analysis first.")
    rx = await rx_svc.draft(layer.db, s.visit, s.client, s.assessment, a.get("items") or [], a.get("rationale"))
    return {"prescription_id": str(rx.id), "version": rx.version, "status": rx.status, "items": rx.items,
            "safety": rx.safety}


async def h_request_approval(layer: "ActionLayer", s: State, a: dict) -> dict:
    ap = await rx_svc.request_approval(layer.db, s.visit)
    return {"approval_id": str(ap.id), "status": ap.status}


async def h_escalate(layer: "ActionLayer", s: State, a: dict) -> dict:
    trigger = a.get("trigger") if a.get("trigger") in ("user_requested", "medical_concern") else "ai_requested"
    esc = await escalation_svc.escalate(layer.db, s.conversation, s.visit, trigger,
                                        str(a.get("details") or "The AI asked for a doctor."))
    return {"escalation_id": str(esc.id), "trigger": esc.trigger}


async def h_add_note(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.client, "no_client", "Identify the client first.")
    note = await summary_svc.add_note(layer.db, s.client, s.visit, a.get("text", ""), a.get("tags") or [])
    return {"note_id": str(note.id), "tags": note.tags}


async def h_share_summary(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.visit, "no_visit", "No visit.")
    return await summary_svc.share(layer.db, s.visit, a.get("channel") or "sms")


async def h_set_next_session(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.visit, "no_visit", "No visit.")
    d = await summary_svc.set_next_session(layer.db, s.visit)
    return {"next_session_date": d.isoformat() if d else None}


async def h_offer_ot_slots(layer: "ActionLayer", s: State, a: dict) -> dict:
    """Find the earliest free slots for the next procedure that needs a room, and remember them as the current offer."""
    _require(s.ot_pending, "nothing_to_book", "No treatments are waiting to be booked.")
    p = s.ot_pending[0]
    item = await layer.db.get(CatalogueItem, p["item_id"])
    day = after = None
    try:
        day = date.fromisoformat(str(a["day"])) if a.get("day") else None
        after = datetime.fromisoformat(str(a["after"])) if a.get("after") else None
    except ValueError:
        raise DomainError("invalid_date", "day must be YYYY-MM-DD and after an ISO time.", 422)
    part = a.get("part") or None
    slots = await ot_svc.find_slots(layer.db, item, day=day, part=part, after=after)
    matched = bool(slots) or not (day or part)
    if not slots and (day or part):
        # Nothing on the day/time asked for: offer the nearest times from that day on instead.
        start = (datetime.combine(day, time(0), ot_svc.clinic_tz()) - timedelta(seconds=1)) if day else after
        slots = await ot_svc.find_slots(layer.db, item, after=start)
    _set_ctx(s, ot_offer={"item_id": item.id, "slots": slots} if slots else None)
    return {"procedure": p, "pending_count": len(s.ot_pending), "slots": slots, "matched": matched,
            "day": day.isoformat() if day else None, "day_label": ot_svc.day_label(day) if day else None, "part": part}


async def h_book_ot_slot(layer: "ActionLayer", s: State, a: dict) -> dict:
    offer = _ctx(s).get("ot_offer") or {}
    _require(s.ot_pending, "nothing_to_book", "No treatments are waiting to be booked.")
    _require(offer.get("slots") and offer.get("item_id") == s.ot_pending[0]["item_id"], "no_offer",
             "Offer slots before booking one.")
    slots = offer["slots"]
    idx = a.get("slot")
    label = str(a.get("label") or "").strip().lower()
    slot = None
    if isinstance(idx, int) and 1 <= idx <= len(slots):
        slot = slots[idx - 1]
    elif label:
        slot = next((x for x in slots if x["label"].lower() == label), None)
    _require(slot, "unknown_slot", "Pick one of the offered times.")
    item = await layer.db.get(CatalogueItem, offer["item_id"])
    b = await ot_svc.book(layer.db, visit=s.visit, item=item, room_id=uuid.UUID(slot["room_id"]),
                          starts_at=datetime.fromisoformat(slot["starts_at"]), user_id=layer.user_id, source="ai")
    _set_ctx(s, ot_offer=None)
    from app.models import OtRoom
    return {**ot_svc.booking_out(b, item, await layer.db.get(OtRoom, b.room_id)), "remaining": len(s.ot_pending) - 1}


async def h_defer_ot_booking(layer: "ActionLayer", s: State, a: dict) -> dict:
    _require(s.ot_pending, "nothing_to_book", "No treatments are waiting to be booked.")
    item = await layer.db.get(CatalogueItem, s.ot_pending[0]["item_id"])
    b = await ot_svc.defer(layer.db, visit=s.visit, item=item, user_id=layer.user_id, source="ai")
    _set_ctx(s, ot_offer=None)
    return {"procedure": item.name, "booking_id": str(b.id), "status": b.status, "remaining": len(s.ot_pending) - 1}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("send_otp", "Send a one-time code to the client's mobile number to verify it.",
         {"properties": {"mobile": {"type": "string", "description": "The 10-digit mobile number the client gave."}},
          "required": ["mobile"]}, {"identify"}, h_send_otp),
    Tool("verify_otp", "Check the 6-digit code the client read out.",
         {"properties": {"code": {"type": "string"}}, "required": ["code"]}, {"identify"}, h_verify_otp),
    Tool("find_or_import_client",
         "After OTP verification: find the client, import from D365, or create them. If it returns needs_profile, call again with full_name, dob and gender; if needs_dob, call again with dob.",
         {"properties": {"full_name": {"type": "string"}, "dob": {"type": "string", "description": "YYYY-MM-DD"},
                         "gender": {"type": "string", "enum": ["female", "male", "other"]}}}, {"identify"},
         h_find_or_import_client),
    Tool("start_visit", "Start the visit from the client's concern in their own words. Picks programme and session number.",
         {"properties": {"concern_text": {"type": "string"}}, "required": ["concern_text"]}, {"concern"}, h_start_visit),
    Tool("save_assessment", "Save the client's answer to the CURRENT Know your Skin question only, as {question_id: option value} (or text for text questions).",
         {"properties": {"answers": {"type": "object", "additionalProperties": {"type": "string"}}}, "required": ["answers"]},
         {"assess"}, h_save_assessment),
    Tool("upload_photo", "Store a guided-capture photo after quality checks.", {}, {"capture"}, h_upload_photo, llm=False),
    Tool("run_analysis", "Run diagnosis and anti-ageing analysis on the accepted photos.", {}, {"analyse"}, h_run_analysis),
    Tool("get_recommendations", "Get catalogue recommendations from the analysis, concerns, skin type and clinic rules, after safety checks.",
         {}, {"recommend"}, h_get_recommendations),
    Tool("draft_prescription", "Draft the prescription. Items must come from get_recommendations (catalogue ids only).",
         {"properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
             "catalogue_id": {"type": "string"}, "sessions": {"type": "integer"}, "instructions": {"type": "string"},
             "reason": {"type": "string"}}, "required": ["catalogue_id"]}},
             "rationale": {"type": "string"}}, "required": ["items"]}, {"recommend"}, h_draft_prescription),
    Tool("request_approval", "Send the draft prescription to a doctor for approval.", {}, {"recommend"}, h_request_approval),
    Tool("escalate_to_doctor", "Hand the visit to a doctor when you are unsure or something is outside skincare.",
         {"properties": {"details": {"type": "string"}}, "required": ["details"]}, ACTIVE, h_escalate),
    Tool("add_note", "Write a visit note with short tags.",
         {"properties": {"text": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
          "required": ["text"]}, AFTER_CLIENT, h_add_note),
    Tool("share_summary", "Share the approved visit summary with the client.",
         {"properties": {"channel": {"type": "string", "enum": ["sms", "whatsapp", "link"]}}}, {"wrap_up", "done"},
         h_share_summary),
    Tool("set_next_session", "Set the next session date from the approved plan.", {}, {"wrap_up"}, h_set_next_session),
    Tool("offer_ot_slots", "Find free procedure-room slots for the next treatment in the approved plan.",
         {"properties": {"day": {"type": "string"}, "part": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                         "after": {"type": "string"}}}, {"booking"}, h_offer_ot_slots, llm=False),
    Tool("book_ot_slot", "Book one of the offered slots for the client.",
         {"properties": {"slot": {"type": "integer"}, "label": {"type": "string"}}}, {"booking"}, h_book_ot_slot, llm=False),
    Tool("defer_ot_booking", "The client will book this treatment later with the front desk.", {}, {"booking"},
         h_defer_ot_booking, llm=False),
]}


def llm_tools_for(stage: str) -> list[dict]:
    return [t.schema() for t in TOOLS.values() if t.llm and stage in t.stages]


class ActionLayer:
    def __init__(self, db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID | None = None):
        self.db = db
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.escalations: list[str] = []
        self.via_llm = False

    async def call(self, name: str, args: dict | None, reason: str, actor: str = "ai", via_llm: bool = False) -> dict:
        args = dict(args or {}) if isinstance(args, dict) else {}
        args.pop("reason", None)
        s = await load_state(self.db, self.conversation_id)
        stage = s.stage
        tool = TOOLS.get(name)
        logged_inputs = _redact_inputs(args)
        if tool is None or (via_llm and not tool.llm):
            result = {"ok": False, "status": "blocked", "error": f"{name} is not an action you can call."}
        elif stage not in tool.stages:
            why = "a doctor has taken over this visit" if stage == "handed_over" else f"not allowed in stage '{stage}'"
            result = {"ok": False, "status": "blocked", "error": f"{name} is {why}."}
        else:
            try:
                self.via_llm = via_llm
                async with self.db.begin_nested():  # a failed action leaves no partial changes
                    data = await tool.handler(self, s, args)
                result = {"ok": True, "status": "ok", "data": jsonable_encoder(data)}
            except DomainError as e:
                s = await load_state(self.db, self.conversation_id)
                result = {"ok": False, "status": "error", "error": e.message, "code": e.code}
            except Exception as e:  # noqa: BLE001 — unexpected failures are still audited (NFR-02)
                log.exception("action %s failed", name)
                s = await load_state(self.db, self.conversation_id)
                result = {"ok": False, "status": "error", "error": "That action failed unexpectedly.", "code": type(e).__name__}
        self.db.add(AIAction(
            conversation_id=self.conversation_id, visit_id=s.conversation.visit_id, action=name, actor=actor,
            inputs=jsonable_encoder(logged_inputs), result=_trim(result), reason=(reason or "")[:1000] or "(none given)",
            status=result["status"], stage=stage,
        ))
        await self.db.flush()
        # Escalation rules run in code after every action.
        s = await load_state(self.db, self.conversation_id)
        new = await escalation_svc.check_and_escalate(self.db, s.conversation, s.visit, s.client, s.assessment, s.analyses)
        for esc in new:
            self.escalations.append(esc.trigger)
            self.db.add(AIAction(conversation_id=self.conversation_id, visit_id=s.conversation.visit_id,
                                 action="escalate_to_doctor", actor="automation", inputs={"trigger": esc.trigger},
                                 result={"escalation_id": str(esc.id)}, status="ok", stage=stage,
                                 reason=f"Rule: {escalation_svc.TRIGGER_TEXT.get(esc.trigger, esc.trigger)}"))
        if new:
            result["escalated"] = [e.trigger for e in new]
        await self.db.commit()
        return result


def _redact_inputs(args: dict) -> dict:
    """The audit log keeps what was done, not secrets: OTP codes are hidden and mobiles keep the last 4 digits."""
    out = {}
    for k, v in args.items():
        if k == "data":
            out["bytes"] = len(v) if isinstance(v, (bytes, bytearray)) else None
        elif k == "code":
            out[k] = "••••••"
        elif k == "mobile":
            out[k] = mask_mobile(str(v)[-10:]) if v else v
        else:
            out[k] = v
    return out


def _trim(result: dict) -> dict:
    """Keep the audit row compact; big payloads (e.g. full analysis) are stored in their own tables."""
    import json

    raw = json.dumps(result, default=str)
    return result if len(raw) < 8000 else {**{k: v for k, v in result.items() if k != "data"}, "data": "(truncated)"}
