"""Follow-up chats between a client and the clinic (one per past visit, plus one general chat per client).

Who answers is decided here, in code:
- mode "ai": the follow-up assistant answers from the visit's approved plan only (app/ai/followup.py).
- mode "staff": a doctor or admin is handling it; the AI stays quiet until someone hands it back.
The AI moves a chat to "staff" when the client asks for a person, mentions a worrying symptom, or asks something the
approved plan doesn't answer. `waiting_since` marks chats where the client is waiting for the clinic team.

Clients only ever reach their own chats: on the clinic device through the verified conversation, and on their phone
through a client sign-in (mobile + OTP). Staff messages text the client once per unread burst.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.integrations.messaging import get_messenger
from app.models import AIAction, Client, Thread, ThreadMessage, User, Visit
from app.services import DomainError
from app.services.clients import age_of, mask_mobile
from app.services.visits import programme_label

log = logging.getLogger(__name__)

HANDOVER_TEXT = {
    "user_requested": "The client asked to talk to a person.",
    "medical_concern": "The client described a worrying symptom.",
    "out_of_scope": "The client asked for a change the assistant can't make.",
    "unanswered": "The approved plan doesn't answer the client's question.",
    "ai_requested": "The assistant asked for a doctor.",
    "staff_started": "Started by the clinic team.",
    "client_replied": "The client wrote back.",
}
MAX_TEXT = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Lookups ──────────────────────────────────────────────────────────────────
async def get_or_create(db: AsyncSession, client_id: uuid.UUID, visit_id: uuid.UUID | None,
                        created_by: User | None = None) -> Thread:
    """The client's chat about a visit (or their general chat). The visit must be the client's own."""
    if visit_id is not None:
        visit = await db.get(Visit, visit_id)
        if visit is None or visit.client_id != client_id:
            raise DomainError("not_found", "Consultation not found.", 404)

    async def find() -> Thread | None:
        q = select(Thread).where(Thread.client_id == client_id)
        q = q.where(Thread.visit_id == visit_id) if visit_id is not None else q.where(Thread.visit_id.is_(None))
        return (await db.execute(q)).scalar_one_or_none()

    t = await find()
    if t is not None:
        return t
    try:
        async with db.begin_nested():
            t = Thread(client_id=client_id, visit_id=visit_id, mode="ai", last_seq=0, client_read_seq=0,
                       staff_read_seq=0, client_notified_seq=0, created_by_user_id=created_by.id if created_by else None)
            db.add(t)
            await db.flush()
    except IntegrityError:  # opened at the same moment elsewhere
        t = await find()
    return t


async def for_client(db: AsyncSession, thread_id: uuid.UUID, client_id: uuid.UUID, lock: bool = False) -> Thread:
    q = select(Thread).where(Thread.id == thread_id).execution_options(populate_existing=True)
    if lock:
        q = q.with_for_update()
    t = (await db.execute(q)).scalar_one_or_none()
    if t is None or t.client_id != client_id:  # never reveal that someone else's chat exists
        raise DomainError("not_found", "Chat not found.", 404)
    return t


async def locked(db: AsyncSession, thread_id: uuid.UUID) -> Thread:
    t = (await db.execute(select(Thread).where(Thread.id == thread_id).with_for_update()
                          .execution_options(populate_existing=True))).scalar_one_or_none()
    if t is None:
        raise DomainError("not_found", "Chat not found.", 404)
    return t


async def messages_after(db: AsyncSession, thread: Thread, after_seq: int = 0) -> list[ThreadMessage]:
    return list((await db.execute(select(ThreadMessage).where(ThreadMessage.thread_id == thread.id,
                                                             ThreadMessage.seq > after_seq)
                                  .order_by(ThreadMessage.seq))).scalars())


# ── Output ───────────────────────────────────────────────────────────────────
def message_out(m: ThreadMessage) -> dict:
    meta = m.meta or {}
    return {"id": str(m.id), "seq": m.seq, "role": m.role, "content": m.content,
            "author": meta.get("author"), "author_role": meta.get("author_role"), "channel": meta.get("channel"),
            "created_at": m.created_at.isoformat()}


def _title(visit: Visit | None) -> tuple[str, str | None]:
    if visit is None:
        return "Messages with the clinic", None
    return f"{programme_label(visit.programme)}, session {visit.session_number}", visit.created_at.date().isoformat()


async def _unread(db: AsyncSession, threads: list[Thread], for_staff: bool) -> dict[uuid.UUID, int]:
    if not threads:
        return {}
    roles = ["client"] if for_staff else ["assistant", "staff"]
    read_col = Thread.staff_read_seq if for_staff else Thread.client_read_seq
    rows = (await db.execute(
        select(ThreadMessage.thread_id, func.count(ThreadMessage.id))
        .join(Thread, Thread.id == ThreadMessage.thread_id)
        .where(ThreadMessage.thread_id.in_([t.id for t in threads]), ThreadMessage.role.in_(roles),
               ThreadMessage.seq > read_col)
        .group_by(ThreadMessage.thread_id))).all()
    return {tid: n for tid, n in rows}


async def _last(db: AsyncSession, t: Thread) -> dict | None:
    if not t.last_seq:
        return None
    m = (await db.execute(select(ThreadMessage).where(ThreadMessage.thread_id == t.id, ThreadMessage.seq == t.last_seq))
         ).scalar_one_or_none()
    if m is None:
        return None
    return {"role": m.role, "author": (m.meta or {}).get("author"), "content": m.content[:140],
            "created_at": m.created_at.isoformat()}


async def thread_out(db: AsyncSession, t: Thread, unread: int = 0, for_staff: bool = False) -> dict:
    visit = await db.get(Visit, t.visit_id) if t.visit_id else None
    title, date = _title(visit)
    out = {"id": str(t.id), "visit_id": str(t.visit_id) if t.visit_id else None, "title": title, "date": date,
           "mode": t.mode, "waiting": t.waiting_since is not None, "unread": unread,
           "last_message": await _last(db, t),
           "last_message_at": t.last_message_at.isoformat() if t.last_message_at else None}
    if for_staff:
        client = await db.get(Client, t.client_id)
        assigned = await db.get(User, t.assigned_user_id) if t.assigned_user_id else None
        out.update({
            "client": {"id": str(client.id), "full_name": client.full_name, "age": age_of(client),
                       "gender": client.gender, "mobile": mask_mobile(client.mobile)},
            "waiting_since": t.waiting_since.isoformat() if t.waiting_since else None,
            "handover_reason": t.handover_reason,
            "handover_text": HANDOVER_TEXT.get(t.handover_reason or "", None),
            "handover_details": t.handover_details,
            "assigned_to": assigned.full_name if assigned else None,
            "visit": ({"id": str(visit.id), "status": visit.status, "programme": programme_label(visit.programme),
                       "session_number": visit.session_number, "date": visit.created_at.date().isoformat()}
                      if visit else None),
        })
    return out


async def list_for_client(db: AsyncSession, client_id: uuid.UUID) -> list[dict]:
    threads = list((await db.execute(select(Thread).where(Thread.client_id == client_id, Thread.last_seq > 0)
                                     .order_by(Thread.last_message_at.desc().nulls_last()))).scalars())
    unread = await _unread(db, threads, for_staff=False)
    return [await thread_out(db, t, unread.get(t.id, 0)) for t in threads]


async def unread_for_client(db: AsyncSession, client_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count(ThreadMessage.id)).join(Thread, Thread.id == ThreadMessage.thread_id)
        .where(Thread.client_id == client_id, ThreadMessage.role.in_(["assistant", "staff"]),
               ThreadMessage.seq > Thread.client_read_seq)) or 0


async def list_for_staff(db: AsyncSession, waiting_only: bool = False, client_id: uuid.UUID | None = None,
                         q: str | None = None, limit: int = 100) -> list[dict]:
    stmt = select(Thread).join(Client, Client.id == Thread.client_id).where(Thread.last_seq > 0)
    if waiting_only:
        stmt = stmt.where(Thread.waiting_since.is_not(None))
    if client_id is not None:
        stmt = stmt.where(Thread.client_id == client_id)
    if q:
        stmt = stmt.where(Client.full_name.ilike(f"%{q}%") | Client.mobile.like(f"%{q}%"))
    stmt = stmt.order_by(Thread.waiting_since.asc().nulls_last(), Thread.last_message_at.desc().nulls_last()).limit(limit)
    threads = list((await db.execute(stmt)).scalars())
    unread = await _unread(db, threads, for_staff=True)
    return [await thread_out(db, t, unread.get(t.id, 0), for_staff=True) for t in threads]


# ── Writing ──────────────────────────────────────────────────────────────────
async def add(db: AsyncSession, t: Thread, role: str, content: str, meta: dict | None = None,
              author_user_id: uuid.UUID | None = None, client_message_id: str | None = None) -> ThreadMessage:
    """Caller holds the thread row lock (`locked` / `for_client(lock=True)`), so seq numbers can't collide."""
    t.last_seq = (t.last_seq or 0) + 1
    t.last_message_at = _now()
    m = ThreadMessage(thread_id=t.id, seq=t.last_seq, role=role, content=content.strip()[:4000], meta=meta or {},
                      author_user_id=author_user_id, client_message_id=client_message_id)
    db.add(m)
    await db.flush()
    return m


def clean_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        raise DomainError("empty_message", "Please type a message.", 422)
    if len(t) > MAX_TEXT:
        raise DomainError("message_too_long", f"Please keep messages under {MAX_TEXT} characters.", 422)
    return t


async def audit(db: AsyncSession, t: Thread, action: str, actor: str, result: dict, reason: str) -> None:
    db.add(AIAction(conversation_id=None, visit_id=t.visit_id, action=action, actor=actor,
                    inputs={"thread_id": str(t.id)}, result=result, reason=reason, status="ok", stage="follow_up"))
    await db.flush()


async def hand_over(db: AsyncSession, t: Thread, code: str, details: str, actor: str = "ai") -> None:
    t.mode = "staff"
    if t.waiting_since is None:
        t.waiting_since = _now()
    t.handover_reason = code
    t.handover_details = (details or "")[:1000]
    await audit(db, t, "followup_handover", actor, {"reason": code}, HANDOVER_TEXT.get(code, code))


async def existing_by_cmid(db: AsyncSession, t: Thread, cmid: str | None) -> ThreadMessage | None:
    if not cmid:
        return None
    return (await db.execute(select(ThreadMessage).where(ThreadMessage.thread_id == t.id,
                                                        ThreadMessage.client_message_id == cmid))).scalar_one_or_none()


async def client_post(db: AsyncSession, thread_id: uuid.UUID, client: Client, text: str, cmid: str | None,
                      channel: str) -> dict:
    """The client writes. Stored and committed first; then the AI answers if the chat is still the AI's."""
    from app.ai import followup
    from app.ai.orchestrator import conv_lock

    text = clean_text(text)
    async with conv_lock(thread_id):
        t = await for_client(db, thread_id, client.id, lock=True)
        dup = await existing_by_cmid(db, t, cmid)
        if dup is not None:  # a retried send: return what that send produced
            msgs = await messages_after(db, t, dup.seq - 1)
            await db.commit()
            return {"thread": await thread_out(db, t), "messages": [message_out(m) for m in msgs]}
        first_seq = t.last_seq + 1
        await add(db, t, "client", text, {"channel": channel, "author": client.full_name.split()[0]},
                  client_message_id=cmid)
        t.client_read_seq = t.last_seq
        ai_turn = t.mode == "ai"
        if not ai_turn and t.waiting_since is None:
            t.waiting_since = _now()
            t.handover_reason = "client_replied"  # the clinic team has this chat and the client wrote back
            t.handover_details = None
        await db.commit()

        if ai_turn:
            result = await followup.answer(db, t, client, text)
            t = await locked(db, thread_id)
            if t.mode == "ai":  # staff may have stepped in while the AI was thinking
                for r in result.replies:
                    await add(db, t, "assistant", r, {"author": "AI assistant", "agent": result.agent})
                if result.handover:
                    await hand_over(db, t, result.handover[0], result.handover[1])
                else:
                    await audit(db, t, "followup_reply", "ai", {"agent": result.agent}, "Answered from the approved plan")
                t.client_read_seq = t.last_seq  # the client is looking at the chat as the answer arrives
            await db.commit()
        msgs = await messages_after(db, t, first_seq - 1)
        return {"thread": await thread_out(db, t), "messages": [message_out(m) for m in msgs]}


async def mark_client_read(db: AsyncSession, t: Thread) -> None:
    if t.client_read_seq != t.last_seq:
        t.client_read_seq = t.last_seq
        await db.commit()


async def mark_staff_read(db: AsyncSession, t: Thread) -> None:
    if t.staff_read_seq != t.last_seq:
        t.staff_read_seq = t.last_seq
        await db.commit()


def _author(user: User) -> tuple[str, str]:
    if user.role == "doctor":
        name = user.full_name if user.full_name.lower().startswith("dr") else f"Dr {user.full_name}"
        return name, "doctor"
    return user.full_name, "clinic team"


async def staff_post(db: AsyncSession, thread_id: uuid.UUID, user: User, text: str) -> dict:
    text = clean_text(text)
    from app.ai.orchestrator import conv_lock

    async with conv_lock(thread_id):
        t = await locked(db, thread_id)
        author, role = _author(user)
        m = await add(db, t, "staff", text, {"author": author, "author_role": role}, author_user_id=user.id)
        if t.mode != "staff":
            await audit(db, t, "followup_staff_joined", "staff", {"user": user.full_name}, f"{author} joined the chat")
        t.mode = "staff"
        t.waiting_since = None
        t.assigned_user_id = user.id
        t.staff_read_seq = t.last_seq
        if t.handover_reason is None:
            t.handover_reason = "staff_started"
        notified = await _notify_client(db, t, author)
        await db.commit()
        return {"message": message_out(m), "notified": notified, "thread": await thread_out(db, t, for_staff=True)}


async def _notify_client(db: AsyncSession, t: Thread, author: str) -> bool:
    """Text the client about a staff reply, once until they've read it (no SMS storm for a run of messages)."""
    if t.client_notified_seq > t.client_read_seq:
        return False
    client = await db.get(Client, t.client_id)
    base = get_settings().public_base_url.rstrip("/")
    where = f"Read and reply: {base}/my" if base else "Sign in on the clinic's website with this number to read and reply."
    try:
        await get_messenger().send("sms", client.mobile,
                                   f"Hi {client.full_name.split()[0]}, {author} has sent you a message about your skin "
                                   f"care. {where}")
    except Exception:  # noqa: BLE001 - a failed text must not lose the message itself
        log.warning("follow-up SMS failed")
        return False
    t.client_notified_seq = t.last_seq
    return True


async def hand_back(db: AsyncSession, thread_id: uuid.UUID, user: User) -> dict:
    from app.ai.orchestrator import conv_lock

    async with conv_lock(thread_id):
        t = await locked(db, thread_id)
        if t.mode == "ai":
            raise DomainError("already_ai", "The assistant is already answering this chat.")
        t.mode = "ai"
        t.waiting_since = None
        t.handover_reason = None
        t.handover_details = None
        m = await add(db, t, "system", "The assistant can answer questions about your plan again. Ask for the clinic "
                                       "team any time.")
        t.staff_read_seq = t.last_seq
        await audit(db, t, "followup_hand_back", "staff", {"user": user.full_name}, "Handed back to the assistant")
        await db.commit()
        return {"message": message_out(m), "thread": await thread_out(db, t, for_staff=True)}
