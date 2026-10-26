"""Two agents behind one interface (design decision D5).

- ScriptedAgent: deterministic, rule-based. Used offline, in tests, and as the fallback when the LLM fails.
- LLMAgent: OpenAI-compatible tool calling (Groq by default).

Both act only through ActionLayer, so stage guards, audit logging and escalation rules are identical.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import EVENT_INSTRUCTIONS, build_system_prompt
from app.ai.providers import ChatProvider, LLMError, get_provider
from app.ai.tools import ActionLayer
from app.config import get_settings
from app.services import assessment as assessment_svc
from app.services.analysis import CONCERN_LABELS, top_findings
from app.services.clients import extract_mobile, mask_mobile
from app.services.photos import POSE_PROMPTS, missing_poses
from app.services.state import State, load_state

log = logging.getLogger(__name__)


@dataclass
class TurnContext:
    db: AsyncSession
    layer: ActionLayer
    conversation_id: object

    async def state(self) -> State:
        return await load_state(self.db, self.conversation_id)

    async def set_ctx(self, **updates) -> None:
        s = await self.state()
        ctx = dict(s.conversation.context or {})
        for k, v in updates.items():
            if v is None:
                ctx.pop(k, None)
            else:
                ctx[k] = v
        s.conversation.context = ctx
        await self.db.flush()


# ── Shared phrasing ──────────────────────────────────────────────────────────
GREETING = ("Hello! I'm your AI skin assistant. I'll guide you through today's consultation, and a doctor will review "
            "your plan before it's final. To begin, please tell me your 10-digit mobile number.")

SEVERITY_WORDS = {"minimal": "very little", "mild": "mild", "moderate": "moderate", "severe": "marked"}

ESCALATION_REASON = {
    "under_18": "for clients under 18, a doctor leads the consultation",
    "pregnancy": "some treatments aren't suitable during pregnancy, so a doctor should guide you",
    "breastfeeding": "some treatments aren't suitable while breast-feeding, so a doctor should guide you",
    "medication_conflict": "one of your medicines can interact with some treatments",
    "non_skincare_finding": "I noticed something in your photo that a doctor should look at",
    "low_confidence": "I'm not confident enough in the photo analysis",
    "user_requested": "you asked to speak to a person",
    "doctor_rejected": "the doctor would like to discuss your plan with you directly",
    "ai_requested": "a doctor is better placed to help with this",
    "medical_concern": "what you've described needs a doctor to look at it properly",
}

ACKS = ["Got it.", "Thanks.", "Okay, noted.", "Thank you.", "Great, thanks.", "Noted."]
FILLER = {"sorry", "ok", "okay", "hmm", "hm", "what", "huh", "?", "pardon", "wait", "one sec", "hold on", "yes", "no idea"}


def format_question(q: dict, done: int, total: int) -> str:
    text = q["text"]
    if q["type"] == "choice":
        labels = [o["label"] for o in q["options"]]
        text += " You can say: " + ", ".join(labels[:-1]) + f", or {labels[-1]}."
    elif q.get("hint"):
        text += f" ({q['hint']}.)"
    return text


def explain_analysis(data: dict, concerns: list[str]) -> str:
    findings = top_findings(data["diagnosis"], concerns, n=3)
    parts = []
    for f in findings:
        label = CONCERN_LABELS.get(f["concern"], f["concern"])
        parts.append(f"{SEVERITY_WORDS.get(f['label'], f['label'])} {label} ({f['score']:.0f} out of 100)")
    text = "Here's what I found: " + "; ".join(parts) + "."
    skin_age = (data.get("anti_aging") or {}).get("skin_age")
    if skin_age:
        text += f" Your estimated skin age is {skin_age}."
    cmp = data.get("comparison")
    if cmp and cmp.get("changes"):
        bits = []
        for c, ch in cmp["changes"].items():
            label = CONCERN_LABELS.get(c, c)
            if ch["direction"] == "better":
                bits.append(f"{label} improved by {ch['improvement_pct']:.0f}%")
            elif ch["direction"] == "worse":
                bits.append(f"{label} is {abs(ch['improvement_pct']):.0f}% higher")
            else:
                bits.append(f"{label} is stable")
        text += f" Compared with your visit on {cmp['previous_date']}: " + ", ".join(bits) + "."
    text += " These are AI observations; the doctor will confirm them."
    return text


def explain_draft(data: dict, show_draft: bool) -> str:
    if not show_draft:
        return "I've prepared a draft plan. A doctor will review it before you see it."
    lines = ["Based on this, I've drafted a plan:"]
    for i in data["items"]:
        qty = f" × {i['sessions']} sessions" if i.get("sessions") and i["sessions"] > 1 else ""
        lines.append(f"• {i['name']}{qty} — {i.get('reason') or ''}".rstrip(" —"))
    excluded = (data.get("safety") or {}).get("excluded") or []
    if excluded:
        lines.append("For your safety I left out " + ", ".join(f"{e['name']} ({e['reason']})" for e in excluded) + ".")
    lines.append("This is a draft. A doctor will review it before it's final.")
    return "\n".join(lines)


def escalation_message(triggers: list[str]) -> str:
    reason = ESCALATION_REASON.get(triggers[0], ESCALATION_REASON["ai_requested"]) if triggers else ESCALATION_REASON["ai_requested"]
    return (f"Thank you for telling me. Because {reason}, I'm handing you over to a doctor now. They can see "
            "everything we've discussed, so you won't need to repeat anything.")


def stage_prompt(s: State) -> str:
    """What to say to move the visit forward from its current stage (used on resume / hand-back)."""
    stage = s.stage
    ctx = s.conversation.context or {}
    if stage == "identify":
        awaiting = ctx.get("awaiting")
        if awaiting == "otp":
            return f"Please tell me the 6-digit code I sent to {mask_mobile(ctx.get('pending_mobile', ''))}."
        if awaiting == "profile_name":
            return "I couldn't find your record, so let's create one. What's your full name?"
        if awaiting == "profile_dob":
            return "What's your date of birth? For example, 11-04-1992."
        if awaiting == "profile_gender":
            return "And your gender: female, male or other?"
        return "Please tell me your 10-digit mobile number."
    if stage == "concern":
        return "What would you like help with today? Tell me in your own words."
    if stage == "assess":
        q = assessment_svc.next_question(s.assessment, s.client)
        done, total = assessment_svc.progress(s.assessment, s.client)
        return format_question(q, done, total) if q else "Thanks."
    if stage == "capture":
        pose = missing_poses(s.photos)[0]
        return (f"Let's take the {pose} photo. {POSE_PROMPTS[pose]} Tap the camera button when you're ready, "
                "or upload a photo if this device has no camera.")
    if stage in ("analyse", "recommend"):
        return "One moment while I prepare your results."
    if stage == "approval":
        return "Your plan is with the doctor for review. I'll let you know here as soon as it's approved."
    if stage == "booking":
        offer = current_offer(s)
        if offer:
            return offer_text({"procedure": s.ot_pending[0], "slots": offer["slots"], "matched": True}, short=True)
        return "Let's book your treatment."
    if stage in ("wrap_up", "done"):
        nxt = s.visit.next_session_date.strftime("%d %B %Y") if s.visit and s.visit.next_session_date else None
        return "You're all set." + (f" Your next session is on {nxt}." if nxt else "") + " Take care!"
    return "A doctor will be with you shortly."


def parse_date(text: str) -> date | None:
    t = text.strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%B %d, %Y"):
        m = re.search(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2} [A-Za-z]+,? \d{4}|[A-Za-z]+ \d{1,2},? \d{4}", t)
        candidate = m.group(0) if m else t
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


# ── Treatment-room booking ───────────────────────────────────────────────────
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_BOOK_LATER = re.compile(r"\b(book later|not now|skip( it)?|call me|i'?ll (call|book|decide|ring)|front desk|decide later|"
                         r"later,? thanks|no thanks?|maybe later|another day)\b")
_MORE = re.compile(r"\b(later dates?|more (times|dates|slots|options)|other (times|dates|slots|options)|any ?thing later|"
                   r"none of (these|them|those)|different (time|day|date)s?|show more)\b")
_ORDINAL = re.compile(r"^(the )?(first|second|third|1st|2nd|3rd|1|2|3)( one| option| slot)?[.!]?$")


def current_offer(s: State) -> dict | None:
    offer = (s.conversation.context or {}).get("ot_offer") or {}
    if s.ot_pending and offer.get("item_id") == s.ot_pending[0]["item_id"] and offer.get("slots"):
        return offer
    return None


def _prose(label: str) -> str:
    return label.replace(", ", " at ")


def _join_or(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" or {items[-1]}"


def offer_text(data: dict, lead: str | None = None, short: bool = False) -> str:
    p = data["procedure"]
    times = _join_or([_prose(x["label"]) for x in data["slots"]])
    tail = " Tap one, ask for later dates, or tell me a day that suits you."
    if short:
        return f"When would you like your {p['name']}? I can do {times}.{tail}"
    if not data.get("matched", True):
        when = " ".join(x for x in [f"on {data['day_label']}" if data.get("day_label") else "",
                                    f"in the {data['part']}" if data.get("part") else ""] if x)
        return f"I don't have a free room {when}. The nearest times are {times}.{tail}"
    if data.get("day_label") or data.get("part"):
        return f"{lead or 'Sure.'} I can do {times}.{tail}"
    sess = f"session 1 of {p['sessions']}, " if p.get("sessions", 1) > 1 else ""
    intro = lead or f"Let's book your {p['name']} ({sess}about {p['duration_min']} minutes)."
    return f"{intro} The earliest times are {times}.{tail}"


def parse_day(text: str, today: date) -> tuple[date | None, str | None]:
    """'saturday afternoon', 'tomorrow', '24 oct', '24/10' → (date, part of day)."""
    t = f" {text.lower()} "
    part = next((p for p in ("morning", "afternoon", "evening") if p in t), None)
    if part is None and re.search(r"after lunch|post lunch", t):
        part = "afternoon"
    d = None
    if "day after tomorrow" in t:
        d = today + timedelta(days=2)
    elif "tomorrow" in t:
        d = today + timedelta(days=1)
    elif re.search(r"\btoday\b", t):
        d = today
    else:
        for i, w in enumerate(WEEKDAYS):
            if re.search(rf"\b({w}|{w[:3]})\b", t):
                ahead = (i - today.weekday()) % 7
                if ahead == 0 and "next" in t:
                    ahead = 7
                d = today + timedelta(days=ahead)
                break
    if d is None:
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3})[a-z]*\b", t) or None
        mm = re.search(r"\b([a-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
        num = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", t)
        day_n = mon = None
        if m and m.group(2) in MONTHS:
            day_n, mon = int(m.group(1)), MONTHS.index(m.group(2)) + 1
        elif mm and mm.group(1) in MONTHS:
            day_n, mon = int(mm.group(2)), MONTHS.index(mm.group(1)) + 1
        elif num:
            day_n, mon = int(num.group(1)), int(num.group(2))
        if day_n and mon:
            try:
                d = date(today.year, mon, day_n)
                if d < today:
                    d = date(today.year + 1, mon, day_n)
            except ValueError:
                d = None
    return d, part


def parse_gender(text: str) -> str | None:
    t = f" {text.lower()} "
    if re.search(r"\b(female|woman|girl|f)\b", t):
        return "female"
    if re.search(r"\b(male|man|boy|m)\b", t):
        return "male"
    if re.search(r"\b(other|non-binary|nonbinary|prefer not)\b", t):
        return "other"
    return None


def clean_name(text: str) -> str:
    t = re.sub(r"^(hi|hello)[,!.\s]+", "", text.strip(), flags=re.I)
    t = re.sub(r"^(my name is|i am|i'm|this is|name is|it's)\s+", "", t, flags=re.I)
    t = re.sub(r"[^A-Za-z .'-]", "", t).strip(" .")
    return " ".join(w.capitalize() for w in t.split())[:120]


# ── Scripted agent ───────────────────────────────────────────────────────────
class ScriptedAgent:
    name = "scripted"

    async def turn(self, ctx: TurnContext, text: str) -> list[str]:
        s = await ctx.state()
        stage = s.stage
        handler = getattr(self, f"_{stage}", None)
        if handler is None:
            return [stage_prompt(s)]
        return await handler(ctx, s, text)

    async def _identify(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        cctx = s.conversation.context or {}
        awaiting = cctx.get("awaiting")
        mobile = extract_mobile(text)
        if mobile and awaiting in (None, "mobile", "otp"):
            r = await ctx.layer.call("send_otp", {"mobile": mobile}, "Client gave their mobile number; verifying it by OTP.")
            if not r["ok"]:
                return [r["error"]]
            hint = " (Demo mode: the code is 123456.)" if r["data"].get("mock") else ""
            return [f"Thanks. I've sent a 6-digit code to {r['data']['to']}. Please tell me the code.{hint}"]
        if awaiting == "otp":
            if "resend" in text.lower() or "again" in text.lower():
                r = await ctx.layer.call("send_otp", {"mobile": cctx["pending_mobile"]}, "Client asked for a new code.")
                return ["I've sent a new code." if r["ok"] else r["error"]]
            m = re.search(r"\b(\d{6})\b", re.sub(r"(?<=\d)[\s-](?=\d)", "", text))
            if not m:
                return ["Please tell me the 6-digit code from the text message."]
            r = await ctx.layer.call("verify_otp", {"code": m.group(1)}, "Client read out the OTP.")
            if not r["ok"]:
                return [r["error"]]
            if not r["data"]["verified"]:
                return ["That code didn't match. Please check the message and try again, or say 'resend'."]
            return await self._lookup(ctx)
        if awaiting == "lookup":
            return await self._lookup(ctx)
        if awaiting == "profile_name":
            name = clean_name(text)
            if len(name) < 2:
                return ["Sorry, what's your full name?"]
            await ctx.set_ctx(profile={"full_name": name}, awaiting="profile_dob")
            return [f"Nice to meet you, {name.split()[0]}. What's your date of birth? For example, 11-04-1992."]
        if awaiting == "profile_dob":
            d = parse_date(text)
            if not d or d >= date.today() or d.year < 1900:
                return ["I didn't catch that date. Please say it like 11-04-1992."]
            if (cctx.get("profile") or {}).get("existing"):
                r = await ctx.layer.call("find_or_import_client", {"dob": d.isoformat()},
                                         "Record had no date of birth; adding the one the client gave.")
                if not r["ok"]:
                    return [r["error"]]
                if r.get("escalated"):
                    return []
                return [f"Thanks, {r['data']['first_name']}. What would you like help with today? Tell me in your own words."]
            await ctx.set_ctx(profile={**cctx.get("profile", {}), "dob": d.isoformat()}, awaiting="profile_gender")
            return ["Thanks. And your gender: female, male or other?"]
        if awaiting == "profile_gender":
            g = parse_gender(text)
            if not g:
                return ["Please say female, male or other."]
            profile = {**cctx.get("profile", {}), "gender": g}
            r = await ctx.layer.call("find_or_import_client", profile, "New client: creating their record from the details they gave.")
            if not r["ok"]:
                return [r["error"]]
            if r.get("escalated"):
                return []
            return [f"Thanks, {r['data']['first_name']}, you're all set up. What would you like help with today? Tell me in your own words."]
        return ["To begin, please tell me your 10-digit mobile number."]

    async def _lookup(self, ctx: TurnContext) -> list[str]:
        r = await ctx.layer.call("find_or_import_client", {}, "Mobile verified; looking up the client record.")
        if not r["ok"]:
            return [r["error"]]
        d = r["data"]
        if d["status"] == "needs_profile":
            return ["Thanks, that's verified. I couldn't find your record, so let's create one. What's your full name?"]
        if d["status"] == "needs_dob":
            return [f"Thanks, {d['first_name']}. Your record doesn't have your date of birth. What is it? For example, 11-04-1992."]
        if r.get("escalated"):
            return []
        welcome = "Welcome back" if d["status"] == "found" else "Thanks"
        lv = d.get("last_visit")
        if lv:
            when = datetime.fromisoformat(lv["date"]).strftime("%d %B %Y").lstrip("0")
            n = d.get("past_visits") or 1
            past = "your past consultation is" if n == 1 else f"all {n} past consultations are"
            return [f"{welcome}, {d['first_name']}! Your last visit was on {when} ({lv['programme']}, session "
                    f"{lv['session_number']}); {past} under the History tab. What would you like help with today? "
                    "Tell me in your own words."]
        return [f"{welcome}, {d['first_name']}! What would you like help with today? Tell me in your own words."]

    async def _concern(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        if len(text.split()) < 3 and not any(k in text.lower() for k in ("acne", "spot", "pigment", "wrinkle", "dry", "oily", "dull", "red", "pore", "tan", "circle")):
            return ["Tell me a little more about what's bothering you about your skin."]
        r = await ctx.layer.call("start_visit", {"concern_text": text}, "Client described their concern; starting the visit.")
        if not r["ok"]:
            return [r["error"]]
        d = r["data"]
        s2 = await ctx.state()
        intro = f"Thank you. I've started your {d['programme_label']}, session {d['session_number']}."
        if d["reused_answers"]:
            intro += f" I've kept {len(d['reused_answers'])} answers from your last visit, so this will be quicker."
        if s2.stage != "assess":
            return [intro, stage_prompt(s2)]
        return [intro + " Now a few quick questions about your skin.", stage_prompt(s2)]

    async def _assess(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        q = assessment_svc.next_question(s.assessment, s.client)
        value = assessment_svc.parse_answer(q, text)
        if value is None:
            done, total = assessment_svc.progress(s.assessment, s.client)
            lead = "No problem, take your time." if text.strip().lower().strip(".!? ") in FILLER else "I didn't quite catch that."
            return [f"{lead} {format_question(q, done, total)}"]
        r = await ctx.layer.call("save_assessment", {"answers": {q["id"]: value}}, f"Client answered {q['id']}.")
        if not r["ok"]:
            return [r["error"]]
        if r.get("escalated"):
            return []
        d = r["data"]
        if d["completed"]:
            s2 = await ctx.state()
            return [assessment_done_message(s2), stage_prompt(s2)]
        s2 = await ctx.state()
        done, _ = assessment_svc.progress(s2.assessment, s2.client)
        return [f"{ACKS[done % len(ACKS)]} {stage_prompt(s2)}"]

    async def _capture(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        return [stage_prompt(s) + " If you can't take photos right now, say \u201ctalk to a doctor\u201d and I'll hand you over."]

    async def _approval(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        return [stage_prompt(s)]

    async def _booking(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        offer = current_offer(s)
        slots = offer["slots"] if offer else []
        low = text.strip().lower()
        if low == "book later" or _BOOK_LATER.search(low):
            return await self.defer_booking(ctx)
        if low == "later dates" or _MORE.search(low):
            after = slots[-1]["starts_at"] if slots else None
            return await self.offer(ctx, {"after": after} if after else {}, lead="Here are some later times.")
        idx = next((i for i, x in enumerate(slots, 1) if x["label"].lower() == low), None)
        if idx is None and _ORDINAL.match(low):
            n = {"first": 1, "1st": 1, "1": 1, "second": 2, "2nd": 2, "2": 2, "third": 3, "3rd": 3, "3": 3}[_ORDINAL.match(low).group(2)]
            idx = n if n <= len(slots) else None
        if idx is None and len(low) >= 4:
            hits = [i for i, x in enumerate(slots, 1) if low in x["label"].lower() or low in _prose(x["label"]).lower()]
            idx = hits[0] if len(hits) == 1 else None
        if idx is not None:
            return await self.book_slot(ctx, idx)
        from app.services.ot import clinic_tz, now_utc

        d, part = parse_day(low, now_utc().astimezone(clinic_tz()).date())
        if d or part:
            return await self.offer(ctx, {k: v for k, v in {"day": d.isoformat() if d else None, "part": part}.items() if v})
        if not offer:
            return await self.offer(ctx, {})
        if looks_like_question(text):
            return ["Good question. The clinic team will go through everything with you before the treatment.",
                    stage_prompt(s)]
        return [f"I didn't quite catch that. {stage_prompt(s)}"]

    async def offer(self, ctx: TurnContext, args: dict, lead: str | None = None) -> list[str]:
        r = await ctx.layer.call("offer_ot_slots", args, "Client asked for " + (
            ", ".join(f"{k}={v}" for k, v in args.items() if k != "after") or "other times") + ".")
        if not r["ok"]:
            return [r["error"]]
        if not r["data"]["slots"]:
            return ["I couldn't find any later times in the next few weeks. Pick one of the times above, or say "
                    "\u201cbook later\u201d and the front desk will call you."]
        return [offer_text(r["data"], lead)]

    async def book_slot(self, ctx: TurnContext, idx: int) -> list[str]:
        r = await ctx.layer.call("book_ot_slot", {"slot": idx}, f"Client chose offered slot {idx}.")
        if r["ok"]:
            b = r["data"]
            return [f"I've held {_prose(b['label'])} in the {b['room_name']} for your {b['procedure']}. It's provisional "
                    "until the clinic team confirms it, and we'll text you when they do."]
        if r.get("code") == "slot_taken":
            return ["Sorry, that time has just been taken."] + await self.offer(ctx, {}, lead="Here are the next free times.")
        return [r["error"]]

    async def defer_booking(self, ctx: TurnContext) -> list[str]:
        r = await ctx.layer.call("defer_ot_booking", {}, "Client wants to book this treatment later.")
        if not r["ok"]:
            return [r["error"]]
        return [f"No problem. The front desk will call you to book your {r['data']['procedure']}."]

    async def _wrap_up(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        return [stage_prompt(s)]

    async def _done(self, ctx: TurnContext, s: State, text: str) -> list[str]:
        if extract_mobile(text):
            return ["This consultation is already finished. To start a new one, tap \u201cEnd session\u201d and begin again."]
        return [stage_prompt(s)]

    async def on_event(self, ctx: TurnContext, event: str, data: dict) -> list[str]:
        s = await ctx.state()
        if event == "session_start":
            return [GREETING]
        if event == "photo_feedback":
            if not data["accepted"]:
                return [f"{data['guidance']} Let's retake the {data['pose']} photo."]
            if data["missing_poses"]:
                nxt = data["missing_poses"][0]
                return [f"Great, that one's clear. Now the {nxt} photo: {POSE_PROMPTS[nxt]}"]
            return ["All three photos look good. Analysing your skin now…"]
        if event == "analysis_done":
            return [explain_analysis(data, list(s.visit.concerns or []))]
        if event == "draft_ready":
            return [explain_draft(data, get_settings().client_sees_draft_plan)]
        if event == "approval_requested":
            return ["I've sent it to the doctor for review. This usually takes a few minutes; I'll update you here."]
        if event == "approved" and data.get("booking_needed"):
            edited = " with a few changes" if data.get("edited") else ""
            n = len(data.get("procedures") or [])
            what = "your treatment" if n == 1 else f"your {n} treatments"
            return [f"Good news: {data.get('doctor', 'the doctor')} has approved your plan{edited}. Before you go, "
                    f"let's book {what} in one of our procedure rooms."]
        if event == "ot_offer":
            return [offer_text(data)]
        if event == "ot_none":
            return [f"I couldn't find a free room for your {data['procedure']['name']} in the next few weeks, so the "
                    "front desk will call you to arrange it."]
        if event == "wrapped_up":
            appts = data.get("appointments") or []
            booked = [a for a in appts if a["status"] in ("tentative", "confirmed")]
            provisional = any(a["status"] == "tentative" for a in booked)
            later = [a["procedure"] for a in appts if a["status"] == "deferred"]
            parts = []
            if booked:
                parts.append(("Your times are held: " if provisional else "You're all set: ")
                             + "; ".join(f"{a['procedure']} on {_prose(a['label'])}" for a in booked) + "."
                             + (" The clinic team will confirm them and text you." if provisional else ""))
            if later:
                parts.append(f"The front desk will call you to book {_join_or(later).replace(' or ', ' and ')}.")
            if data.get("shared_to"):
                parts.append(f"I've sent your summary{' and appointment' if booked else ''} to your mobile {data['shared_to']}.")
            nxt = data.get("next_session_date")
            if nxt and not booked:
                parts.append(f"Your next session is on {datetime.fromisoformat(nxt).strftime('%d %B %Y')}.")
            return [" ".join(parts + ["Do you have any questions?"])]
        if event == "approved":
            edited = " with a few changes" if data.get("edited") else ""
            nxt = data.get("next_session_date")
            nxt_txt = f" Your next session is on {datetime.fromisoformat(nxt).strftime('%d %B %Y')}." if nxt else ""
            to = data.get("shared_to")
            share_txt = f" I've sent the summary to your mobile {to}." if to else ""
            return [f"Good news: {data.get('doctor', 'the doctor')} has approved your plan{edited}.{share_txt}{nxt_txt} "
                    "Do you have any questions?"]
        if event == "escalated":
            return [escalation_message(data.get("triggers", []))]
        if event == "handed_back":
            return ["The doctor has handed you back to me. Let's continue.", stage_prompt(s)]
        return []


# ── LLM agent ────────────────────────────────────────────────────────────────
# Steps that must behave exactly the same every time run in code, even when an LLM is configured:
# identity/OTP (no phone numbers or codes are sent to the LLM), and photo capture, analysis and drafting
# (the LLM can't claim a photo was taken or a plan exists).
DETERMINISTIC_STAGES = {"identify", "capture", "analyse", "recommend"}

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060-\u2064\ufeff]")
_ODD_SPACES = re.compile(r"[\u00a0\u2007\u2009\u200a\u202f]")  # narrow/no-break spaces → normal space
_QUESTION_START = re.compile(r"^(what|why|how|when|where|which|who|can|could|should|would|is|are|do|does|will|tell me)\b", re.I)


def clean_reply(text: str | None) -> str | None:
    """Strip invisible characters and markdown emphasis; return None if the reply looks garbled."""
    if not text:
        return None
    t = _ODD_SPACES.sub(" ", _ZERO_WIDTH.sub("", text)).replace("**", "")
    lines = [ln.rstrip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln.strip(" .…-—/)(") or not ln.strip()]  # drop punctuation-only lines
    t = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not t:
        return None
    visible = [c for c in t if not c.isspace()]
    alnum = sum(c.isalnum() for c in visible)
    lines = [ln for ln in t.splitlines() if ln.strip()]
    short_lines = sum(1 for ln in lines if len(ln.split()) <= 2)
    fragmented = len(lines) > 1 and short_lines * 2 >= len(lines)
    if alnum / max(1, len(visible)) < 0.7 or t.count("…") + t.count("...") >= 2 or fragmented:
        return None
    return t


def looks_like_question(text: str) -> bool:
    t = text.strip()
    return t.endswith("?") or bool(_QUESTION_START.match(t))


def assessment_done_message(s: State) -> str:
    a = s.assessment
    st = (a.skin_type if a else None) or "normal"
    sens = ", and it tends to be sensitive" if a and a.sensitive else ""
    return (f"Thanks, that's everything. Your skin type looks {st}{sens}. Next I'll take three photos of your face. "
            "Hold the device at eye level, in good light, without make-up if possible.")


_PHOTO_WORDS = re.compile(r"\b(photo|photos|picture|pictures|pic|capture|captured|camera|selfie|left|right|front|side|upload)")
_PHOTO_PROGRESS = re.compile(r"\b(taken|took|received|got (it|them|your)|next|now (take|turn|the)|done|looks? good|clear)\b")
_PERSON_WORDS = re.compile(r"\b(doctor|dr|dermatologist|person|human|someone|somebody|staff|nurse|real|people|agent)\b")


def drop_restated_step(reply: str | None, q: dict | None) -> str | None:
    """The app asks the next question itself, straight after the reply. Models still like to re-ask it ("Let me know how
    visible your pores are: Barely visible, ..."), which shows the question twice; drop those sentences."""
    if not reply:
        return reply
    labels = [o["label"].lower() for o in (q or {}).get("options", [])]
    kept = []
    for sent in re.split(r"(?<=[.!?])\s+", reply.strip()):
        low = sent.lower()
        if sent.rstrip().endswith("?") or sum(lab in low for lab in labels) >= 2:
            continue
        kept.append(sent)
    return " ".join(kept).strip() or None


def parse_decision(raw: str | None) -> dict:
    """Pull the JSON object out of the model's reply; tolerate stray text around it."""
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"intent": "other", "reply": raw}
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {"intent": "other", "reply": raw[: m.start()] or ""}


def is_exact_option(q: dict | None, text: str) -> bool:
    if not q:
        return False
    t = text.strip().lower()
    if q["type"] == "text":
        return t.strip(" .!") in assessment_svc.NONE_WORDS
    return any(t in (o["label"].lower(), o["value"].lower()) for o in q["options"])


class LLMAgent:
    """Conversational layer. On each typed reply the model decides what the client meant and writes a short, human reply;
    code then acts (save the answer, start the visit, hand over) through the ActionLayer and adds the next step verbatim.

    Identity/OTP stay in code (no phone numbers or codes go to the model), tapped options skip the model, and the model
    has no way to take photos, draft plans or approve anything.
    """

    def __init__(self, provider: ChatProvider):
        self.provider = provider
        self.name = f"llm:{provider.name}"
        self.fallback = ScriptedAgent()

    async def _history(self, ctx: TurnContext, s: State, limit: int = 24) -> list[dict]:
        from sqlalchemy import select

        from app.models import Message

        rows = (await ctx.db.execute(select(Message).where(Message.conversation_id == s.conversation.id)
                                     .order_by(Message.seq.desc()).limit(limit))).scalars().all()
        out = []
        for m in reversed(rows):
            if "••••" in m.content or "6-digit code" in m.content:
                continue  # identity/OTP turns stay out of the model's context
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role in ("assistant", "doctor"):
                out.append({"role": "assistant", "content": ("[Doctor] " if m.role == "doctor" else "") + m.content})
        return out

    async def _decide(self, ctx: TurnContext, s: State, text: str, next_step: str | None) -> dict:
        from app.ai.prompts import build_converse_prompt, client_memory

        memory = await client_memory(ctx.db, s)
        history = await self._history(ctx, s)
        if not history or history[-1].get("content") != text:
            history.append({"role": "user", "content": text})
        msg = await self.provider.chat([{"role": "system", "content": build_converse_prompt(s, memory, next_step)}] + history,
                                       temperature=0.4)
        return parse_decision(msg.get("content"))

    async def _hand_over(self, ctx: TurnContext, trigger: str, reply: str | None, text: str) -> list[str]:
        r = await ctx.layer.call("escalate_to_doctor", {"details": text[:300], "trigger": trigger},
                                 f"Client message needs a doctor ({trigger}).", via_llm=True)
        out = [reply] if reply else []
        return out + [escalation_message([trigger])] if r["ok"] else out + [stage_prompt(await ctx.state())]

    async def turn(self, ctx: TurnContext, text: str) -> list[str]:
        s = await ctx.state()
        stage = s.stage
        if stage == "identify" or (stage == "done" and extract_mobile(text)):
            return await self.fallback.turn(ctx, text)
        q = assessment_svc.next_question(s.assessment, s.client) if stage == "assess" else None
        if stage == "assess" and is_exact_option(q, text):
            return await self.fallback.turn(ctx, text)  # a tapped option: fast, no model call
        offer = current_offer(s) if stage == "booking" else None
        if stage == "booking":
            labels = {x["label"].lower() for x in (offer or {}).get("slots", [])}
            if text.strip().lower() in labels | {"later dates", "book later"} or not offer:
                return await self.fallback.turn(ctx, text)  # a tapped time or button
            q = {"options": [{"label": x["label"]} for x in offer["slots"]]}
        next_step = stage_prompt(s) if stage in ("concern", "assess", "capture", "booking") else None
        try:
            d = await self._decide(ctx, s, text, next_step)
        except LLMError as e:
            log.warning("LLM unavailable: %s", type(e).__name__)
            return await self.fallback.turn(ctx, text)
        intent = str(d.get("intent") or "other").lower()
        value = str(d.get("value") or "").strip()
        reply = clean_reply(str(d.get("reply") or ""))
        if next_step:
            reply = drop_restated_step(reply, q)

        if intent == "human" and not _PERSON_WORDS.search(text.lower()):
            intent = "other"  # the model over-read the message; only hand over when they actually ask for a person
        if intent == "human":
            return await self._hand_over(ctx, "user_requested", reply, text)
        if intent == "medical":
            return await self._hand_over(ctx, "medical_concern", reply, text)

        if stage == "concern" and intent == "concern":
            r = await ctx.layer.call("start_visit", {"concern_text": text, "summary": value},
                                     f"Client described their concern: {value or text}"[:300],
                                     via_llm=True)
            after = await ctx.state()
            if r["ok"] and after.visit is not None and after.stage != "handed_over":
                from app.services.visits import programme_label
                intro = (f"I've started your {programme_label(after.visit.programme)}, session {after.visit.session_number}. "
                         "First, a few quick questions about your skin.")
                return [f"{reply} {intro}" if reply else intro, stage_prompt(after)]
            return [reply] if reply else []

        if stage == "assess" and intent == "answer" and q:
            v = None
            if q["type"] == "choice":
                v = value if value in {o["value"] for o in q["options"]} else assessment_svc.parse_answer(q, value)
            elif value:
                v = value
            if v:
                r = await ctx.layer.call("save_assessment", {"answers": {q["id"]: v}}, f"Client said: {text[:120]}", via_llm=True)
                after = await ctx.state()
                if r["ok"]:
                    if after.stage == "assess":
                        return [f"{reply} {stage_prompt(after)}" if reply else f"{ACKS[0]} {stage_prompt(after)}"]
                    if after.stage == "capture":
                        return ([reply] if reply else []) + [assessment_done_message(after), stage_prompt(after)]
                    return [reply] if reply else []

        if stage == "booking" and offer:
            # Times and bookings are only ever stated by code, from the real room calendar.
            fb = self.fallback
            if intent == "slot":
                idx = next((i for i, x in enumerate(offer["slots"], 1) if x["label"].lower() == value.lower()), None)
                if idx:
                    return await fb.book_slot(ctx, idx)
            elif intent == "more":
                return await fb.offer(ctx, {"after": offer["slots"][-1]["starts_at"]}, lead="Here are some later times.")
            elif intent == "defer":
                return await fb.defer_booking(ctx)
            elif intent == "date":
                from app.services.ot import clinic_tz, now_utc

                today = now_utc().astimezone(clinic_tz()).date()
                m = re.search(r"\d{4}-\d{2}-\d{2}", value)
                d = None
                if m:
                    try:
                        d = date.fromisoformat(m.group(0))
                    except ValueError:
                        d = None
                if d is None or d < today:
                    d = parse_day(text, today)[0]
                part = next((p for p in ("morning", "afternoon", "evening") if p in value.lower()), None) or parse_day(text, today)[1]
                if d or part:
                    return await fb.offer(ctx, {k: v for k, v in {"day": d.isoformat() if d else None, "part": part}.items() if v})

        # Anything else: a kind, relevant reply, then put the current step back (never skipped, never faked).
        after = await ctx.state()
        if after.stage == "booking" and reply and re.search(r"\b(booked|reserved|confirmed|scheduled|blocked)\b", reply.lower()):
            reply = None  # only code says a time is booked
        asked = looks_like_question(text) and reply and not _PHOTO_PROGRESS.search(reply.lower())
        if after.stage == "capture" and not asked and (not reply or _PHOTO_WORDS.search(reply.lower())):
            # Photo progress is only ever stated by code, from what was actually uploaded.
            reply = f"I don't have the {missing_poses(after.photos)[0]} photo yet."
        out = [reply] if reply else []
        if after.stage in ("concern", "assess", "capture", "booking"):
            out.append(stage_prompt(after))
        elif not out and after.stage != "handed_over":
            out.append(stage_prompt(after))
        return out

    async def on_event(self, ctx: TurnContext, event: str, data: dict) -> list[str]:
        if event == "escalated":
            return await self._escalated(ctx, data)
        instruction = EVENT_INSTRUCTIONS.get(event)
        if event == "approved" and data.get("booking_needed"):
            instruction = None  # nothing has been sent yet; the fixed wording says what happens next
        if instruction is None or (event == "draft_ready" and not get_settings().client_sees_draft_plan):
            return await self.fallback.on_event(ctx, event, data)
        s = await ctx.state()
        try:
            msg = await self.provider.chat([
                {"role": "system", "content": build_system_prompt(s)},
                {"role": "user", "content": f"EVENT: {event}\nDATA: {json.dumps(data, default=str)[:6000]}\n\n{instruction}"},
            ])
            content = clean_reply(msg.get("content"))
            return [content] if content else await self.fallback.on_event(ctx, event, data)
        except LLMError:
            return await self.fallback.on_event(ctx, event, data)

    async def _escalated(self, ctx: TurnContext, data: dict) -> list[str]:
        """A human touch before the (fixed) hand-over message."""
        template = escalation_message(data.get("triggers", []))
        s = await ctx.state()
        try:
            history = await self._history(ctx, s, limit=6)
            msg = await self.provider.chat([
                {"role": "system", "content": "You are a warm clinic assistant. Write ONE short sentence that kindly acknowledges "
                                              "the client's last message. Do not give advice, do not mention doctors or next steps. "
                                              "Plain text, British spelling."}] + history)
            line = clean_reply(msg.get("content"))
            if line and len(line) < 220:
                return [line, template]
        except LLMError:
            pass
        return [template]


def get_agent():
    provider = get_provider()
    return LLMAgent(provider) if provider else ScriptedAgent()
