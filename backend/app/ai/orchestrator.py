"""Conversation orchestrator: turns, photo uploads, automations and doctor events (docs/02 §4)."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agents import TurnContext, get_agent, stage_prompt
from app.ai.tools import ActionLayer
from app.models import Conversation, Escalation, Message, User, Visit
from app.services import assessment as assessment_svc
from app.services import escalation as escalation_svc
from app.services import summary as summary_svc
from app.services.analysis import latest, top_findings
from app.services.photos import POSE_PROMPTS, accepted_by_pose, missing_poses
from app.services.state import STAGES, State, load_state
from app.services.visits import programme_label

log = logging.getLogger(__name__)

# One turn at a time per conversation, held for the whole turn (a DB row lock is released at the first commit).
# With several API workers, replace this with a Postgres advisory lock or a Redis lock.
_locks: dict[uuid.UUID, asyncio.Lock] = {}


def conv_lock(conv_id: uuid.UUID) -> asyncio.Lock:
    if conv_id not in _locks:
        if len(_locks) > 5000:
            for k in [k for k, v in _locks.items() if not v.locked()][:2500]:
                _locks.pop(k, None)
        _locks[conv_id] = asyncio.Lock()
    return _locks[conv_id]


SORRY = "Sorry, something went wrong on my side. Please say that again."

STAGE_ORDER = ["identify", "concern", "assess", "capture", "analyse", "recommend", "approval", "booking", "wrap_up", "done"]
BOOKING_CHIPS = ["Later dates", "Book later"]


# ── Messages ─────────────────────────────────────────────────────────────────
async def add_message(db: AsyncSession, conversation_id: uuid.UUID, role: str, content: str, meta: dict | None = None,
                      client_message_id: str | None = None) -> Message:
    seq = (await db.scalar(select(func.max(Message.seq)).where(Message.conversation_id == conversation_id))) or 0
    m = Message(conversation_id=conversation_id, seq=seq + 1, role=role, content=content, meta=meta or {},
                client_message_id=client_message_id)
    db.add(m)
    await db.flush()
    return m


def message_out(m: Message) -> dict:
    return {"id": str(m.id), "seq": m.seq, "role": m.role, "content": m.content, "meta": m.meta,
            "created_at": m.created_at.isoformat()}


async def _store_replies(db: AsyncSession, conv_id: uuid.UUID, texts: list[str], reply_to: str | None,
                         agent_name: str) -> list[Message]:
    out = []
    for t in texts:
        if t and t.strip():
            out.append(await add_message(db, conv_id, "assistant", t.strip(), {"reply_to": reply_to, "agent": agent_name}))
    return out


def _mask_identifiers(text: str) -> str:
    """The transcript keeps no OTP codes and only the last 4 digits of a mobile number."""
    import re

    from app.services.clients import extract_mobile, mask_mobile

    mobile = extract_mobile(text)
    if mobile:
        text = re.sub(r"\+?\d[\d\s-]{8,15}\d", mask_mobile(mobile), text, count=1)
    return re.sub(r"\b\d{6}\b", "••••••", text)


# ── UI hints ─────────────────────────────────────────────────────────────────
def build_ui(s: State) -> dict:
    stage = s.stage
    conv = s.conversation
    ctx = conv.context or {}
    ui: dict = {
        "stage": stage, "mode": conv.mode,
        "step": STAGE_ORDER.index(stage) + 1 if stage in STAGE_ORDER else None, "steps": len(STAGE_ORDER),
        "input": "text", "quick_replies": [], "question": None, "camera": None,
        "client_first_name": s.client.full_name.split()[0] if s.client else None,
        "visit_id": str(s.visit.id) if s.visit else None,
        "programme": programme_label(s.visit.programme) if s.visit else None,
        "session_number": s.visit.session_number if s.visit else None,
        "results_available": latest(s.analyses, "diagnosis") is not None,
        "approval_status": s.approval.status if s.approval else None,
        "summary_available": bool(s.prescription and s.prescription.status == "approved"),
    }
    if stage == "identify":
        awaiting = ctx.get("awaiting")
        ui["input"] = {"otp": "otp", "profile_dob": "date", "profile_name": "text"}.get(awaiting or "", "tel")
        if awaiting == "profile_gender":
            ui["quick_replies"] = ["Female", "Male", "Other"]
        if awaiting == "otp":
            ui["quick_replies"] = ["Resend code"]
    elif stage == "assess" and s.client:
        q = assessment_svc.next_question(s.assessment, s.client)
        done, total = assessment_svc.progress(s.assessment, s.client)
        if q:
            ui["question"] = {"id": q["id"], "text": q["text"], "index": done + 1, "total": total, "type": q["type"]}
            ui["quick_replies"] = [o["label"] for o in q["options"]] if q["type"] == "choice" else ["None"]
    elif stage == "capture":
        missing = missing_poses(s.photos)
        ui["camera"] = {"pose": missing[0], "prompt": POSE_PROMPTS[missing[0]], "missing": missing,
                        "done": list(accepted_by_pose(s.photos).keys())}
    elif stage == "booking" and s.ot_pending:
        offer = ctx.get("ot_offer") or {}
        p = s.ot_pending[0]
        slots = offer.get("slots") if offer.get("item_id") == p["item_id"] else None
        ui["booking"] = {"procedure": p["name"], "duration_min": p["duration_min"], "sessions": p["sessions"],
                         "remaining": len(s.ot_pending), "slots": slots or []}
        ui["quick_replies"] = [x["label"] for x in slots or []] + BOOKING_CHIPS
    elif stage in ("wrap_up", "done"):
        ui["quick_replies"] = ["No, thank you"]
    return ui


async def turn_payload(db: AsyncSession, conv_id: uuid.UUID, messages: list[Message]) -> dict:
    from app.services.history import count_past

    s = await load_state(db, conv_id)
    ui = build_ui(s)
    ui["history_count"] = await count_past(db, s.client.id, s.visit.id if s.visit else None) if s.client else 0
    if s.client:
        from app.services.threads import unread_for_client

        ui["unread_messages"] = await unread_for_client(db, s.client.id)
    else:
        ui["unread_messages"] = 0
    return {"conversation_id": str(conv_id), "messages": [message_out(m) for m in messages], "ui": ui}


# ── Automations (design decision D4) ─────────────────────────────────────────
async def _wrap_up(ctx: TurnContext) -> dict:
    """Everything is approved and booked: set the next session, share the summary and close the visit."""
    from app.services import ot as ot_svc

    layer = ctx.layer
    nxt = await layer.call("set_next_session", {}, "Plan approved; setting the next session.", actor="automation")
    shared = await layer.call("share_summary", {"channel": "sms"}, "Sharing the approved summary with the client.",
                              actor="automation")
    s = await ctx.state()
    ok = bool(nxt["ok"] and shared["ok"])
    if ok:
        await summary_svc.complete_visit(ctx.db, s.visit)
    else:
        log.warning("wrap-up incomplete; visit left approved for staff follow-up")
    await ctx.db.commit()
    return {"ok": ok, "next_session_date": (nxt.get("data") or {}).get("next_session_date"),
            "shared_to": (shared.get("data") or {}).get("destination"),
            "appointments": await ot_svc.visit_appointments(ctx.db, s.visit) if s.visit else []}


async def run_automations(ctx: TurnContext, agent) -> list[str]:
    out: list[str] = []
    for _ in range(6):
        s = await ctx.state()
        stage = s.stage
        before = len(ctx.layer.escalations)
        if stage == "booking":
            offer = (s.conversation.context or {}).get("ot_offer") or {}
            if offer.get("item_id") == s.ot_pending[0]["item_id"]:
                break  # the client is choosing from the current offer
            r = await ctx.layer.call("offer_ot_slots", {}, "Plan approved; offering times for the next treatment.",
                                     actor="automation")
            if not r["ok"]:
                break
            if r["data"]["slots"]:
                out += await agent.on_event(ctx, "ot_offer", r["data"])
                break
            # No suitable room free within the horizon: hand the booking to the front desk and move on.
            d = await ctx.layer.call("defer_ot_booking", {}, "No free room within the booking horizon.", actor="automation")
            if not d["ok"]:
                break
            out += await agent.on_event(ctx, "ot_none", r["data"])
            continue
        if stage == "wrap_up":
            w = await _wrap_up(ctx)
            if w["ok"]:
                out += await agent.on_event(ctx, "wrapped_up", w)
            break
        if stage == "analyse":
            r = await ctx.layer.call("run_analysis", {}, "All required photos passed quality checks.", actor="automation")
            if not r["ok"]:
                break
            if len(ctx.layer.escalations) > before:
                out += await agent.on_event(ctx, "escalated", {"triggers": ctx.layer.escalations[before:]})
                break
            out += await agent.on_event(ctx, "analysis_done", r["data"])
            continue
        if stage == "recommend":
            rx = s.prescription
            if rx is None or rx.status == "rejected":
                rec = await ctx.layer.call("get_recommendations", {}, "Analysis complete; building catalogue recommendations.",
                                           actor="automation")
                if not rec["ok"]:
                    break
                if not rec["data"]["items"]:
                    await ctx.layer.call("escalate_to_doctor", {"details": "No suitable catalogue items after safety checks."},
                                         "Nothing safe to recommend; a doctor must decide.", actor="automation")
                    out += await agent.on_event(ctx, "escalated", {"triggers": ["ai_requested"]})
                    break
                findings = ", ".join(rec["data"]["ranked_concerns"][:3])
                d = await ctx.layer.call("draft_prescription", {
                    "items": rec["data"]["items"],
                    "rationale": f"Drafted from analysis and concerns ({findings}); skin type {rec['data']['skin_type']}.",
                }, "Drafting the plan from catalogue recommendations.", actor="automation")
                if not d["ok"]:
                    break
                out += await agent.on_event(ctx, "draft_ready", d["data"])
            ap = await ctx.layer.call("request_approval", {}, "Draft passed safety checks; every plan needs doctor approval.",
                                      actor="automation")
            if ap["ok"]:
                out += await agent.on_event(ctx, "approval_requested", {})
            break
        break
    return out


# ── Entry points ─────────────────────────────────────────────────────────────
async def start_conversation(db: AsyncSession, operator: User, language: str) -> dict:
    now = datetime.now(timezone.utc)
    conv = Conversation(operator_user_id=operator.id, language=language, mode="ai", consent_at=now,
                        consent_recording_at=now, context={"awaiting": "mobile"})
    db.add(conv)
    await db.flush()
    agent = get_agent()
    ctx = TurnContext(db, ActionLayer(db, conv.id, operator.id), conv.id)
    texts = await agent.on_event(ctx, "session_start", {})
    msgs = await _store_replies(db, conv.id, texts, None, agent.name)
    await db.commit()
    return await turn_payload(db, conv.id, msgs)


async def handle_user_message(db: AsyncSession, conv_id: uuid.UUID, text: str, client_message_id: str,
                              user: User) -> dict:
    async with conv_lock(conv_id):
        return await _handle_user_message(db, conv_id, text, client_message_id, user)


async def _handle_user_message(db: AsyncSession, conv_id: uuid.UUID, text: str, client_message_id: str,
                               user: User) -> dict:
    existing = (await db.execute(select(Message).where(Message.conversation_id == conv_id,
                                                       Message.client_message_id == client_message_id))).scalar_one_or_none()
    if existing:  # exactly-once: replay the replies to this message
        replies = (await db.execute(select(Message).where(Message.conversation_id == conv_id,
                                                          Message.meta["reply_to"].astext == client_message_id)
                                    .order_by(Message.seq))).scalars().all()
        return {**await turn_payload(db, conv_id, list(replies)), "duplicate": True}
    s = await load_state(db, conv_id, lock=True)
    if s.conversation.mode == "closed":
        return await turn_payload(db, conv_id, [])
    try:
        stored = _mask_identifiers(text) if s.stage == "identify" else text
        await add_message(db, conv_id, "user", stored[:4000], {}, client_message_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return {**await turn_payload(db, conv_id, []), "duplicate": True}

    agent = get_agent()
    layer = ActionLayer(db, conv_id, user.id)
    ctx = TurnContext(db, layer, conv_id)
    s = await ctx.state()
    texts: list[str] = []

    if s.conversation.mode == "handed_over":
        pass  # the doctor is handling the conversation; the AI stays silent
    else:
        # Escalation rules run on the raw user message too (e.g. "I want to talk to a doctor").
        ignore = ""
        if s.stage == "assess" and s.client:
            q = assessment_svc.next_question(s.assessment, s.client)
            q = q or {}
            ignore = " ".join([q.get("text", ""), q.get("hint", "")] + [o["label"] for o in q.get("options", [])])
        new = await escalation_svc.check_and_escalate(db, s.conversation, s.visit, s.client, s.assessment, s.analyses,
                                                      user_text=text, medical_ignore=ignore)
        if new:
            from app.models import AIAction

            for esc in new:
                db.add(AIAction(conversation_id=conv_id, visit_id=s.conversation.visit_id, action="escalate_to_doctor",
                                actor="automation", inputs={"trigger": esc.trigger}, result={"escalation_id": str(esc.id)},
                                status="ok", stage=s.stage, reason=f"Rule: {escalation_svc.TRIGGER_TEXT[esc.trigger]}"))
            await db.commit()
            texts = await agent.on_event(ctx, "escalated", {"triggers": [e.trigger for e in new]})
        else:
            before = len(layer.escalations)
            try:
                texts = await agent.turn(ctx, text)
                if len(layer.escalations) > before:
                    texts += await agent.on_event(ctx, "escalated", {"triggers": layer.escalations[before:]})
                else:
                    texts += await run_automations(ctx, agent)
            except Exception:  # noqa: BLE001 — the user always gets an answer to a stored message
                log.exception("turn failed")
                await db.rollback()
                texts = [SORRY]
    msgs = await _store_replies(db, conv_id, texts, client_message_id, agent.name)
    await db.commit()
    return await turn_payload(db, conv_id, msgs)


async def handle_photo(db: AsyncSession, conv_id: uuid.UUID, pose: str, data: bytes, content_type: str,
                       user: User) -> dict:
    async with conv_lock(conv_id):
        return await _handle_photo(db, conv_id, pose, data, content_type, user)


async def _handle_photo(db: AsyncSession, conv_id: uuid.UUID, pose: str, data: bytes, content_type: str,
                        user: User) -> dict:
    agent = get_agent()
    layer = ActionLayer(db, conv_id, user.id)
    ctx = TurnContext(db, layer, conv_id)
    r = await layer.call("upload_photo", {"pose": pose, "data": data, "content_type": content_type},
                         f"Guided capture of the {pose} photo.", actor="ai")
    texts: list[str] = []
    quality = None
    if not r["ok"]:
        texts = [r["error"] if r.get("status") == "blocked" or r.get("code", "").islower()
                 else "I couldn't process that photo. Please try again or choose a different one."]
    else:
        quality = r["data"]
        texts = await agent.on_event(ctx, "photo_feedback", r["data"])
        if layer.escalations:
            texts += await agent.on_event(ctx, "escalated", {"triggers": layer.escalations})
        elif r["data"]["accepted"] and not r["data"]["missing_poses"]:
            texts += await run_automations(ctx, agent)
    msgs = await _store_replies(db, conv_id, texts, f"photo:{pose}", agent.name)
    await db.commit()
    return {**await turn_payload(db, conv_id, msgs), "quality": quality, "photo_ok": bool(r["ok"] and quality and quality["accepted"])}


async def latest_conversation_for_visit(db: AsyncSession, visit_id: uuid.UUID) -> Conversation | None:
    return (await db.execute(select(Conversation).where(Conversation.visit_id == visit_id)
                             .order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()


def _note_text(s: State, doctor: User | None) -> tuple[str, list[str]]:
    v, a = s.visit, s.assessment
    parts = [f"AI consultation, {programme_label(v.programme)} session {v.session_number}."]
    if v.concern_text:
        parts.append(f'Concern: "{v.concern_text[:300]}".')
    if a and a.skin_type:
        parts.append(f"Skin type: {a.skin_type}{' (sensitive)' if a.sensitive else ''}.")
    diag = latest(s.analyses, "diagnosis")
    if diag:
        tops = top_findings(diag.result["diagnosis"], list(v.concerns or []))
        parts.append("Findings: " + ", ".join(f"{f['concern']} {f['score']:.0f} ({f['label']})" for f in tops) + ".")
    if s.prescription:
        parts.append("Plan: " + "; ".join(i["name"] + (f" ×{i['sessions']}" if i.get("sessions") else "")
                                         for i in s.prescription.items) + ".")
        excluded = (s.prescription.safety or {}).get("excluded") or []
        if excluded:
            parts.append("Excluded for safety: " + ", ".join(e["name"] for e in excluded) + ".")
    if doctor:
        parts.append(f"Approved by {doctor.full_name}.")
    tags = [v.programme] + list(v.concerns or [])
    if a and a.skin_type:
        tags.append(f"{a.skin_type}_skin")
    if a and a.sensitive:
        tags.append("sensitive")
    return " ".join(parts), tags


async def on_approval_decided(db: AsyncSession, visit: Visit, decision: str, doctor: User) -> list[dict]:
    conv = await latest_conversation_for_visit(db, visit.id)
    if conv is None:
        return []
    async with conv_lock(conv.id):
        return await _on_approval_decided(db, visit, conv, decision, doctor)


async def _on_approval_decided(db: AsyncSession, visit: Visit, conv: Conversation, decision: str, doctor: User) -> list[dict]:
    agent = get_agent()
    layer = ActionLayer(db, conv.id, doctor.id)
    ctx = TurnContext(db, layer, conv.id)
    texts: list[str] = []
    if decision in ("approve", "edit"):
        # The doctor has taken responsibility for the plan; any open escalation on this visit is resolved by it.
        s = await ctx.state()
        for esc in await escalation_svc.open_escalations(db, s.conversation, s.visit):
            await escalation_svc.resolve(db, esc, s.conversation, s.visit, hand_back_to_ai=True)
        # Whatever the hand-over history (incl. "closed without AI"), an approved plan is wrapped up by the AI.
        if s.conversation.mode in ("handed_over", "closed"):
            s.conversation.mode = "ai"
        if s.visit.status == "escalated":
            s.visit.status = "approved"
        await db.commit()
        s = await ctx.state()
        text, tags = _note_text(s, doctor)
        await layer.call("add_note", {"text": text, "tags": tags}, "Recording visit notes from the conversation.",
                         actor="automation")
        s = await ctx.state()
        if s.stage == "booking":
            # Treatments need a procedure room: book them with the client first, then wrap up.
            texts = await agent.on_event(ctx, "approved", {
                "doctor": doctor.full_name, "edited": decision == "edit", "booking_needed": True,
                "procedures": [p["name"] for p in s.ot_pending]})
            texts += await run_automations(ctx, agent)
        else:
            w = await _wrap_up(ctx)
            texts = await agent.on_event(ctx, "approved", {
                "doctor": doctor.full_name, "edited": decision == "edit",
                "next_session_date": w["next_session_date"], "shared_to": w["shared_to"]})
    else:
        s = await ctx.state()
        if s.conversation.mode == "ai":
            s.conversation.mode = "handed_over"
        texts = await agent.on_event(ctx, "escalated", {"triggers": ["doctor_rejected"]})
    msgs = await _store_replies(db, conv.id, texts, f"approval:{decision}", agent.name)
    await db.commit()
    return [message_out(m) for m in msgs]


async def doctor_joined(db: AsyncSession, conv: Conversation, doctor: User) -> Message:
    if conv.mode == "ai":
        conv.mode = "handed_over"
    m = await add_message(db, conv.id, "system", f"{doctor.full_name} has joined the conversation.", {"doctor_id": str(doctor.id)})
    await db.commit()
    return m


async def hand_back(db: AsyncSession, esc: Escalation, doctor: User) -> list[dict]:
    conv = await db.get(Conversation, esc.conversation_id) if esc.conversation_id else None
    if conv is None and esc.visit_id:
        conv = await latest_conversation_for_visit(db, esc.visit_id)
    if conv is None:
        await escalation_svc.resolve(db, esc, None, None, hand_back_to_ai=True)
        await db.commit()
        return []
    async with conv_lock(conv.id):
        return await _hand_back(db, esc, conv, doctor)


async def _hand_back(db: AsyncSession, esc: Escalation, conv: Conversation, doctor: User) -> list[dict]:
    s = await load_state(db, conv.id) if conv else None
    await escalation_svc.resolve(db, esc, s.conversation if s else None, s.visit if s else None, hand_back_to_ai=True)
    await db.commit()
    if conv is None:
        return []
    s = await load_state(db, conv.id)
    msgs: list[Message] = []
    if s.conversation.mode == "ai":
        agent = get_agent()
        ctx = TurnContext(db, ActionLayer(db, conv.id, doctor.id), conv.id)
        texts = await agent.on_event(ctx, "handed_back", {})
        texts += await run_automations(ctx, agent)
        msgs = await _store_replies(db, conv.id, texts, f"handback:{esc.id}", agent.name)
        await db.commit()
    return [message_out(m) for m in msgs]


async def on_ot_changed(db: AsyncSession, visit: Visit, user: User) -> list[dict]:
    """Staff booked, deferred or cancelled a procedure for this visit: let the live conversation move on."""
    conv = await latest_conversation_for_visit(db, visit.id)
    if conv is None:
        return []
    async with conv_lock(conv.id):
        s = await load_state(db, conv.id)
        if s.conversation.mode != "ai" or s.stage not in ("booking", "wrap_up"):
            return []
        if s.stage == "booking":
            # A staff change can make the current offer stale; drop it so a fresh one is made.
            ctx_ = dict(s.conversation.context or {})
            ctx_.pop("ot_offer", None)
            s.conversation.context = ctx_
            await db.commit()
        agent = get_agent()
        ctx = TurnContext(db, ActionLayer(db, conv.id, user.id), conv.id)
        texts = await run_automations(ctx, agent)
        msgs = await _store_replies(db, conv.id, texts, f"ot:{uuid.uuid4()}", agent.name)
        await db.commit()
        return [message_out(m) for m in msgs]


__all__ = ["start_conversation", "on_ot_changed", "handle_user_message", "handle_photo", "on_approval_decided", "doctor_joined",
           "hand_back", "add_message", "message_out", "build_ui", "turn_payload", "stage_prompt", "STAGES"]
