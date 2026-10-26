"""System prompt for the LLM agent. Context is rebuilt from the database on every step."""
import json

from app.config import get_settings
from app.services import assessment as assessment_svc
from app.services.analysis import latest, top_findings
from app.services.clients import age_of, mask_mobile
from app.services.photos import missing_poses
from app.services.state import State
from app.services.visits import programme_label

STAGE_GOALS = {
    "identify": ("Greet briefly. Ask for the client's 10-digit mobile number, call send_otp, then ask for the 6-digit code "
                 "and call verify_otp. When verified, call find_or_import_client. If it returns needs_profile, ask for "
                 "full name, date of birth and gender (one at a time), then call find_or_import_client again with them. If it "
                 "returns needs_dob, ask for the date of birth and call it again with dob (YYYY-MM-DD)."),
    "concern": "Ask what the client wants help with, in their own words. Then call start_visit with their words.",
    "assess": ("The client is answering NEXT_QUESTION. If their reply answers it, map it to one option value and call "
               "save_assessment with that question only. If they asked something instead, answer it briefly in one or two "
               "sentences and do not repeat the question (the app shows it again). Never guess an answer."),
    "capture": ("Photos are taken with the camera in the app. Tell the client which pose is next (MISSING_POSES) and how to "
                "hold the phone. Do not call any photo tool yourself."),
    "analyse": "Analysis runs automatically. Reassure the client briefly.",
    "recommend": "The plan is drafted automatically from the clinic catalogue. Answer questions briefly.",
    "approval": ("A doctor is reviewing the draft plan. Answer questions about the findings in plain words. Never say the plan "
                 "is final or approved."),
    "booking": "The plan is approved. The client is choosing a time for a treatment in a procedure room.",
    "wrap_up": "The plan is approved. Answer questions, then close warmly.",
    "done": "The visit is complete. Answer simple questions and say goodbye.",
}

RULES = """You are the AI skin consultation assistant at a dermatology clinic in India.
Rules you must follow:
- Speak in short, warm, plain sentences (1-3 sentences unless explaining results). British spelling. One question at a time.
- You are not a doctor. Never diagnose disease, never prescribe, never promise outcomes. A registered doctor approves every plan.
- Only mention services and products that appear in the data you are given. Never invent items, prices or results.
- Use tools to act; never claim you did something without calling the tool. Every tool call needs a short reason.
- If the client asks for a person, mentions pregnancy, a worrying symptom (bleeding mole, infection, pain), or you are unsure, call escalate_to_doctor.
- Don't repeat the client's mobile number in full or read back health details unnecessarily.
- Only state what CURRENT CONTEXT shows. Never say photos were taken, results are ready, or a plan is approved unless it
  is in the context, and never promise timelines.
- Plain text only: no markdown, no bullet lists of options (the screen shows the options as buttons)."""


def build_system_prompt(s: State) -> str:
    stage = s.stage
    ctx: dict = {"stage": stage, "goal": STAGE_GOALS.get(stage, "")}
    conv_ctx = s.conversation.context or {}
    if stage == "identify":
        ctx["awaiting"] = conv_ctx.get("awaiting") or "mobile"
        if conv_ctx.get("pending_mobile"):
            ctx["otp_sent_to"] = mask_mobile(conv_ctx["pending_mobile"])
        ctx["mobile_verified"] = bool(s.conversation.verified_mobile)
    if s.client:
        ctx["client"] = {"first_name": s.client.full_name.split()[0], "age": age_of(s.client), "gender": s.client.gender}
    if s.visit:
        ctx["visit"] = {"programme": programme_label(s.visit.programme), "session_number": s.visit.session_number,
                        "concern": s.visit.concern_text, "concerns": s.visit.concerns}
    if stage == "assess" and s.client:
        nq = assessment_svc.next_question(s.assessment, s.client)
        done, total = assessment_svc.progress(s.assessment, s.client)
        ctx["progress"] = f"{done}/{total}"
        ctx["NEXT_QUESTION"] = nq
        remaining = [q["id"] for q in assessment_svc.applicable_questions(s.client)
                     if q["id"] not in (s.assessment.answers if s.assessment else {})]
        ctx["remaining_question_ids"] = remaining
    if s.assessment and s.assessment.completed:
        ctx["skin_type"] = s.assessment.skin_type
        ctx["sensitive"] = s.assessment.sensitive
    if stage == "capture":
        ctx["MISSING_POSES"] = missing_poses(s.photos)
    diag = latest(s.analyses, "diagnosis")
    if diag:
        ctx["findings"] = top_findings(diag.result["diagnosis"], list(s.visit.concerns or []), n=4)
        ctx["analysis_engine"] = diag.engine
    if s.prescription and (get_settings().client_sees_draft_plan or s.prescription.status == "approved"):
        ctx["plan"] = {"status": s.prescription.status,
                       "items": [{k: i.get(k) for k in ("name", "kind", "sessions", "reason")} for i in s.prescription.items]}
    return RULES + "\n\nCURRENT CONTEXT (JSON):\n" + json.dumps(ctx, default=str, ensure_ascii=False, separators=(",", ":"))


EVENT_INSTRUCTIONS = {
    "analysis_done": ("Explain these analysis results to the client in plain words (max 5 short sentences). Lead with the "
                      "concerns they raised. Mention scores as 'out of 100' only for the top 2-3 findings. If a comparison "
                      "with the last visit is present, say how things changed. Say these are AI observations that the "
                      "doctor will confirm. Do not recommend anything yet."),
    "draft_ready": ("Explain the draft plan: list each item on its own line starting with '• ', with one short reason. If any "
                    "items were excluded for safety, say so simply. Say clearly that this is a draft and a doctor will "
                    "review it before it is final. Do not add items that are not listed."),
    "approved": ("Tell the client the doctor has approved the plan (say it was adjusted only if edited is true), that the summary has been "
                 "sent to their phone, and the next session date. Ask if they have any questions. Max 4 sentences."),
}


# ── Conversational layer (every typed reply) ─────────────────────────────────
PERSONA = """You are the AI skin consultation assistant at a dermatology clinic in India, chatting with a client on a clinic tablet.
Talk like a warm, attentive person: respond to what they actually said, use their first name now and then, and remember what
they told you earlier (see MEMORY and the conversation). Keep replies short: one or two sentences. British spelling.
Plain text only: no markdown, lists or emojis.
You are not a doctor: never diagnose, prescribe, or promise results or timelines. Only state facts found in MEMORY or STATE.
The app shows NEXT_STEP (the next question or instruction) straight after your reply, so do NOT ask it or repeat it."""

INTENTS = {
    "concern": {
        "concern": "they describe a cosmetic skin concern (acne, spots, pigmentation, dryness, oiliness, ageing, dark circles…); put a one-line summary in value",
        "medical": "an injury or urgent medical problem: burn, cut or wound, bleeding, infection or pus, swelling, fever, severe or sudden pain, allergic reaction, a changing or bleeding mole",
        "human": "they want to talk to a doctor or a person",
        "other": "greeting, small talk, a question, or anything unclear",
    },
    "assess": {
        "answer": "their message answers CURRENT_QUESTION; put the matching option value in value (for text questions, their answer, or 'none')",
        "medical": "an injury or urgent medical problem (burn, wound, bleeding, infection, swelling, fever, severe pain, allergic reaction, changing mole)",
        "human": "they want to talk to a doctor or a person",
        "other": "a question, an apology, small talk, or a reply that doesn't clearly answer (do NOT guess an answer)",
    },
    "booking": {
        "slot": "they choose one of OFFERED_SLOTS; put that slot's label exactly as written in value",
        "date": "they name a day and/or a time of day; value = the date as YYYY-MM-DD (work it out from TODAY) and/or morning, afternoon or evening",
        "more": "they want later or different times without naming a day",
        "defer": "they don't want to book now (will book later, will call, need to check)",
        "medical": "an injury or urgent medical problem (burn, wound, bleeding, infection, swelling, fever, severe pain, allergic reaction, changing mole)",
        "human": "they want to talk to a doctor or a person",
        "other": "a question (e.g. about the treatment or preparation), small talk, or anything unclear; never say a time is booked",
    },
    "default": {
        "medical": "an injury or urgent medical problem (burn, wound, bleeding, infection, swelling, fever, severe pain, allergic reaction, changing mole)",
        "human": "they want to talk to a doctor or a person",
        "other": "anything else: answer their question or respond kindly",
    },
}


def build_converse_prompt(s: State, memory: dict, next_step: str | None) -> str:
    stage = s.stage
    intents = INTENTS.get(stage, INTENTS["default"])
    state: dict = {"stage": stage, "NEXT_STEP": next_step}
    if stage == "assess" and s.client:
        q = assessment_svc.next_question(s.assessment, s.client)
        if q:
            state["CURRENT_QUESTION"] = {"id": q["id"], "text": q["text"], "type": q["type"],
                                         "options": [{"value": o["value"], "label": o["label"]} for o in q.get("options", [])]}
    if stage == "capture":
        state["photos_still_needed"] = missing_poses(s.photos)
        state["note"] = "Photos are only taken with the camera button or the upload button. Typing cannot take a photo."
    if stage == "booking" and s.ot_pending:
        from app.services.ot import clinic_tz, now_utc

        today = now_utc().astimezone(clinic_tz()).date()
        offer = (s.conversation.context or {}).get("ot_offer") or {}
        state["TODAY"] = f"{today:%A} {today.isoformat()}"
        state["TREATMENT"] = {k: s.ot_pending[0][k] for k in ("name", "duration_min", "sessions")}
        state["OFFERED_SLOTS"] = [x["label"] for x in offer.get("slots", [])]
        state["note"] = "Only code books times. Never say a time is booked or free unless it is in OFFERED_SLOTS."
    diag = latest(s.analyses, "diagnosis")
    if diag and s.visit:
        state["findings"] = top_findings(diag.result["diagnosis"], list(s.visit.concerns or []), n=4)
    if s.prescription and (get_settings().client_sees_draft_plan or s.prescription.status == "approved"):
        state["plan"] = {"status": s.prescription.status, "items": [i["name"] for i in s.prescription.items]}
    spec = ", ".join(f'"{k}" = {v}' for k, v in intents.items())
    return (PERSONA + "\n\nMEMORY (what you know about this client):\n" + json.dumps(memory, default=str, ensure_ascii=False)
            + "\n\nSTATE:\n" + json.dumps(state, default=str, ensure_ascii=False)
            + "\n\nDecide what the client's LAST message means and reply. Respond with JSON only, exactly:\n"
            + '{"intent": "<one of: ' + " | ".join(intents) + '>", "value": "<string or empty>", "reply": "<your short reply>"}\n'
            + "Intents: " + spec + ".")


async def client_memory(db, s: State) -> dict:
    """What a human assistant would remember: who the client is, what they said, and their history with the clinic."""
    from sqlalchemy import select

    from app.models import Note, Prescription, Visit

    mem: dict = {}
    if s.client is None:
        return mem
    c = s.client
    mem["client"] = {"first_name": c.full_name.split()[0], "age": age_of(c), "gender": c.gender,
                     "returning": c.source != "ai" or None}
    if s.visit:
        mem["today"] = {"concern_in_their_words": s.visit.concern_text, "programme": programme_label(s.visit.programme),
                        "session": s.visit.session_number}
    if s.assessment and s.assessment.answers:
        from app.knowledge import question as get_q
        answered = {}
        for qid, val in s.assessment.answers.items():
            q = get_q(qid)
            if not q:
                continue
            label = next((o["label"] for o in q.get("options", []) if o["value"] == val), val)
            answered[q["text"]] = label
        mem["answers_so_far"] = answered
        if s.assessment.skin_type:
            mem["skin_type"] = s.assessment.skin_type
    past = (await db.execute(select(Visit).where(Visit.client_id == c.id, Visit.id != (s.visit.id if s.visit else None))
                             .order_by(Visit.created_at.desc()).limit(3))).scalars().all()
    if past:
        history = []
        for v in past:
            rx = (await db.execute(select(Prescription).where(Prescription.visit_id == v.id))).scalar_one_or_none()
            history.append({"date": v.created_at.date().isoformat(), "programme": programme_label(v.programme),
                            "concern": v.concern_text, "plan": [i["name"] for i in rx.items] if rx and rx.status == "approved" else None})
        mem["previous_visits"] = history
        note = (await db.execute(select(Note).where(Note.client_id == c.id).order_by(Note.created_at.desc()).limit(1))).scalar_one_or_none()
        if note:
            mem["last_note"] = note.text[:400]
    return mem
