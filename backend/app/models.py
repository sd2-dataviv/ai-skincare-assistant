"""ORM models. See docs/04-data-model.md."""
import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ── Staff ────────────────────────────────────────────────────────────────────
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # admin | doctor | operator
    registration_no: Mapped[str | None] = mapped_column(String(64))  # medical council reg. no. (doctors)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ── Clients and visits ───────────────────────────────────────────────────────
class Client(TimestampMixin, Base):
    __tablename__ = "clients"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    mobile: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # E.164
    full_name: Mapped[str] = mapped_column(String(200))
    dob: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(20))  # female | male | other
    email: Mapped[str | None] = mapped_column(String(255))
    external_ref: Mapped[str | None] = mapped_column(String(64))  # D365 contact id
    source: Mapped[str] = mapped_column(String(20), default="local")  # local | d365 | ai

    visits: Mapped[list["Visit"]] = relationship(back_populates="client", order_by="Visit.created_at")


class Visit(TimestampMixin, Base):
    __tablename__ = "visits"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    programme: Mapped[str] = mapped_column(String(40))  # service: acne | pigmentation | age | skin | anti_aging | medical (rules.json)
    session_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="in_progress", index=True)
    concern_text: Mapped[str | None] = mapped_column(Text)
    concerns: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    next_session_date: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    client: Mapped[Client] = relationship(back_populates="visits")
    assessment: Mapped["Assessment | None"] = relationship(back_populates="visit", uselist=False)
    photos: Mapped[list["Photo"]] = relationship(back_populates="visit", order_by="Photo.created_at")
    analyses: Mapped[list["Analysis"]] = relationship(back_populates="visit", order_by="Analysis.created_at")
    prescription: Mapped["Prescription | None"] = relationship(back_populates="visit", uselist=False)


class Assessment(Base):
    __tablename__ = "assessments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), unique=True)
    answers: Mapped[dict] = mapped_column(JSONB, default=dict)  # {question_id: value}
    skipped: Mapped[dict] = mapped_column(JSONB, default=dict)  # {question_id: reason}
    skin_type: Mapped[str | None] = mapped_column(String(20))
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    flags: Mapped[dict] = mapped_column(JSONB, default=dict)  # pregnancy, medication_conflict, ...
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    visit: Mapped[Visit] = relationship(back_populates="assessment")


class Photo(TimestampMixin, Base):
    __tablename__ = "photos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), index=True)
    pose: Mapped[str] = mapped_column(String(10))  # front | left | right
    storage_key: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(50))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    quality: Mapped[dict] = mapped_column(JSONB, default=dict)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)

    visit: Mapped[Visit] = relationship(back_populates="photos")


class Analysis(TimestampMixin, Base):
    __tablename__ = "analyses"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), index=True)
    photo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("photos.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))  # diagnosis | anti_aging | compare
    engine: Mapped[str] = mapped_column(String(40))
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    visit: Mapped[Visit] = relationship(back_populates="analyses")


class ModelImage(TimestampMixin, Base):
    """A picture made by one of the clinic's skin models from a visit photo.

    diagnosis_overlay: the photo with what the diagnosis model found marked on it.
    post_treatment_preview: an illustration of the face with the found spots painted out (made in the background;
    status pending -> ready | failed). Doctors and admins see these in review; the client only once the plan is
    approved, and the preview always as an illustration, not a promised result.
    """
    __tablename__ = "model_images"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), index=True)
    photo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("photos.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(30))  # diagnosis_overlay | post_treatment_preview
    pose: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), default="ready")  # pending | ready | failed
    model: Mapped[str] = mapped_column(String(60))
    storage_key: Mapped[str | None] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(50), default="image/jpeg")
    before_storage_key: Mapped[str | None] = mapped_column(String(300))  # preview: the same face crop, untreated
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Prescription(Base):
    __tablename__ = "prescriptions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | pending_approval | approved | rejected
    items: Mapped[list] = mapped_column(JSONB, default=list)
    safety: Mapped[dict] = mapped_column(JSONB, default=dict)  # {warnings: [], excluded: []}
    rationale: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    visit: Mapped[Visit] = relationship(back_populates="prescription")


class Note(TimestampMixin, Base):
    __tablename__ = "notes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    visit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"))
    author_type: Mapped[str] = mapped_column(String(10))  # ai | staff
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)


# ── AI additions (spec §09) ──────────────────────────────────────────────────
class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    operator_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    language: Mapped[str] = mapped_column(String(10), default="en-IN")
    mode: Mapped[str] = mapped_column(String(20), default="ai")  # ai | handed_over | closed
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consent_recording_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_mobile: Mapped[str | None] = mapped_column(String(20))
    context: Mapped[dict] = mapped_column(JSONB, default=dict)  # pending mobile, profile capture, etc.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", order_by="Message.seq")


class Message(TimestampMixin, Base):
    """One transcript line. The spec's Conversation.transcript is the ordered set of these rows."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "client_message_id", name="uq_message_client_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(12))  # user | assistant | doctor | system
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    client_message_id: Mapped[str | None] = mapped_column(String(64))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AIAction(TimestampMixin, Base):
    __tablename__ = "ai_actions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="SET NULL"), index=True)
    visit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("visits.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(20), default="ai")  # ai | automation | staff
    inputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10))  # ok | blocked | error
    stage: Mapped[str | None] = mapped_column(String(20))


class Approval(TimestampMixin, Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), index=True)
    prescription_version: Mapped[int] = mapped_column(Integer)
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | edited | rejected
    notes: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Escalation(TimestampMixin, Base):
    __tablename__ = "escalations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    trigger: Mapped[str] = mapped_column(String(40))
    details: Mapped[str | None] = mapped_column(Text)
    assigned_doctor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | in_progress | resolved
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Follow-up chats ──────────────────────────────────────────────────────────
class Thread(TimestampMixin, Base):
    """A chat between a client and the clinic after (or outside) a consultation: about one past visit, or general
    (visit_id NULL). The AI answers from the visit's approved plan until it hands over or staff step in."""

    __tablename__ = "threads"
    __table_args__ = (
        Index("uq_thread_client_visit", "client_id", "visit_id", unique=True, postgresql_where=text("visit_id IS NOT NULL")),
        Index("uq_thread_client_general", "client_id", unique=True, postgresql_where=text("visit_id IS NULL")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    visit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(10), default="ai")  # ai | staff
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)  # client awaits staff
    handover_reason: Mapped[str | None] = mapped_column(String(40))
    handover_details: Mapped[str | None] = mapped_column(Text)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_read_seq: Mapped[int] = mapped_column(Integer, default=0)
    staff_read_seq: Mapped[int] = mapped_column(Integer, default=0)
    client_notified_seq: Mapped[int] = mapped_column(Integer, default=0)  # last staff message we texted the client about


class ThreadMessage(TimestampMixin, Base):
    __tablename__ = "thread_messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_thread_message_seq"),
        Index("uq_thread_message_cmid", "thread_id", "client_message_id", unique=True,
              postgresql_where=text("client_message_id IS NOT NULL")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(10))  # client | assistant | staff | system
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # author, author_role, channel
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    client_message_id: Mapped[str | None] = mapped_column(String(64))


# ── Clinic knowledge & infrastructure ────────────────────────────────────────
class CatalogueItem(Base):
    __tablename__ = "catalogue_items"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # slug
    kind: Mapped[str] = mapped_column(String(20))  # service | product | doctor_service
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    concerns: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    skin_types: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list)  # empty = all
    contraindications: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    ingredients: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)
    default_sessions: Mapped[int] = mapped_column(Integer, default=1)
    interval_days: Mapped[int] = mapped_column(Integer, default=0)
    usage: Mapped[str] = mapped_column(Text, default="")
    price_inr: Mapped[int] = mapped_column(Integer, default=0)
    room_type: Mapped[str | None] = mapped_column(String(30))  # OT capability a treatment needs (None = no room)
    duration_min: Mapped[int] = mapped_column(Integer, default=0)  # chair time per session
    equipment_type: Mapped[str | None] = mapped_column(String(40))  # machine it needs, e.g. qswitch_laser (None = none)
    needs_doctor: Mapped[bool] = mapped_column(Boolean, default=False)  # a doctor must perform it
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ── Operation theatre / procedure rooms ──────────────────────────────────────
class OtRoom(TimestampMixin, Base):
    __tablename__ = "ot_rooms"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(20), unique=True)  # e.g. OT-1
    name: Mapped[str] = mapped_column(String(100))
    capabilities: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list)  # room types it can host
    open_time: Mapped[time] = mapped_column(Time)  # clinic local time
    close_time: Mapped[time] = mapped_column(Time)
    open_days: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=lambda: [0, 1, 2, 3, 4, 5])  # 0 = Monday
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=15)  # cleaning/turnover after each procedure
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class OtEquipment(TimestampMixin, Base):
    """A machine a treatment needs (laser, LED panel, dermapen…). Not tied to a room: one machine can serve several
    rooms, but only one treatment at a time, plus its own cool-down/sterilisation buffer."""

    __tablename__ = "ot_equipment"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    equipment_type: Mapped[str] = mapped_column(String(40), index=True)
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # False = out of service


class DoctorHours(Base):
    """A doctor's weekly working hours (clinic local time). Several rows per weekday allow split shifts."""

    __tablename__ = "doctor_hours"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)


class ClinicHoliday(Base):
    __tablename__ = "clinic_holidays"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class OtBlock(TimestampMixin, Base):
    """Time a room, a doctor (leave) or a machine (maintenance) can't be used. Exactly one of the three is set."""

    __tablename__ = "ot_blocks"
    __table_args__ = (CheckConstraint("num_nonnulls(room_id, user_id, equipment_id) = 1", name="ck_ot_block_one_target"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    room_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ot_rooms.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ot_equipment.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(200))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class OtBooking(TimestampMixin, Base):
    """A procedure from an approved plan booked into a room (or deferred for the front desk to book)."""

    __tablename__ = "ot_bookings"
    __table_args__ = (
        # One live booking (or deferral) per plan item and session.
        Index("uq_ot_booking_item_session", "visit_id", "catalogue_item_id", "session_index", unique=True,
              postgresql_where=text("status IN ('tentative', 'confirmed', 'deferred')")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    catalogue_item_id: Mapped[str] = mapped_column(ForeignKey("catalogue_items.id"))
    session_index: Mapped[int] = mapped_column(Integer, default=1)
    sessions_total: Mapped[int] = mapped_column(Integer, default=1)
    # tentative (held, e.g. chosen in the chat) | confirmed (staff confirmed) | deferred (to book later) | cancelled.
    # Tentative and confirmed bookings both hold the room, doctor and machine.
    status: Mapped[str] = mapped_column(String(12), index=True)
    room_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ot_rooms.id"), index=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clear_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # ends_at + room turnover buffer
    source: Mapped[str] = mapped_column(String(10), default="ai")  # ai | staff
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ot_equipment.id"), index=True)
    equipment_clear_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # ends_at + machine buffer
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # The day itself: scheduled → checked_in → in_progress → completed, or no_show
    stage: Mapped[str] = mapped_column(String(12), default="scheduled")
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    override_reason: Mapped[str | None] = mapped_column(String(300))  # why staff booked despite soft rules
    overridden_rules: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)  # bumped on every change; stale edits are refused


class OtReadinessItem(Base):
    """Pre-treatment checklist for one booking. The treatment can't start while a mandatory item is open."""

    __tablename__ = "ot_readiness_items"
    __table_args__ = (UniqueConstraint("booking_id", "key", name="uq_ot_readiness_booking_key"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    booking_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ot_bookings.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(120))
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | done | not_applicable | blocked
    note: Mapped[str | None] = mapped_column(String(300))
    position: Mapped[int] = mapped_column(Integer, default=0)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Defence in depth: the database itself refuses two confirmed bookings whose time (incl. turnover) overlaps in a room.
event.listen(Base.metadata, "before_create", DDL("CREATE EXTENSION IF NOT EXISTS btree_gist"))
# The same holds for a doctor (procedure time) and a machine (procedure time plus its own buffer).
HOLDING = "status IN ('tentative', 'confirmed')"
event.listen(OtBooking.__table__, "after_create", DDL(
    "ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_overlap EXCLUDE USING gist "
    f"(room_id WITH =, tstzrange(starts_at, clear_until) WITH &&) WHERE ({HOLDING})"))
event.listen(OtBooking.__table__, "after_create", DDL(
    "ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_doctor_overlap EXCLUDE USING gist "
    f"(doctor_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE ({HOLDING} AND doctor_id IS NOT NULL)"))
event.listen(OtBooking.__table__, "after_create", DDL(
    "ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_equipment_overlap EXCLUDE USING gist "
    f"(equipment_id WITH =, tstzrange(starts_at, equipment_clear_until) WITH &&) "
    f"WHERE ({HOLDING} AND equipment_id IS NOT NULL)"))


class OtpChallenge(TimestampMixin, Base):
    __tablename__ = "otp_challenges"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    mobile: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SummaryShare(TimestampMixin, Base):
    __tablename__ = "summary_shares"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uid)
    visit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("visits.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20))  # sms | whatsapp | link
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    destination_masked: Mapped[str | None] = mapped_column(String(30))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(String(20), default="sent")


class IdempotencyKey(TimestampMixin, Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    scope: Mapped[str] = mapped_column(String(200))  # "METHOD path user"
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSONB)
