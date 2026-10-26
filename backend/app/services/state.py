"""Load everything a turn needs, and derive the consultation stage from data (design decision D1)."""
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Analysis, Approval, Assessment, Client, Conversation, Photo, Prescription, Visit
from app.services.photos import missing_poses

STAGES = ["identify", "concern", "assess", "capture", "analyse", "recommend", "approval", "booking", "wrap_up", "done",
          "handed_over"]


@dataclass
class State:
    conversation: Conversation
    client: Client | None = None
    visit: Visit | None = None
    assessment: Assessment | None = None
    photos: list[Photo] = field(default_factory=list)
    analyses: list[Analysis] = field(default_factory=list)
    prescription: Prescription | None = None
    approval: Approval | None = None
    ot_pending: list[dict] = field(default_factory=list)  # approved procedures still needing a room booking

    @property
    def stage(self) -> str:
        return compute_stage(self)


def compute_stage(s: State) -> str:
    c = s.conversation
    if c.mode == "handed_over" or (s.visit is not None and s.visit.status == "escalated"):
        return "handed_over"
    if c.mode == "closed":
        return "done"
    if s.client is None:
        return "identify"
    if s.visit is None:
        return "concern"
    if s.visit.status == "completed":
        return "done"
    rx = s.prescription
    # A doctor-approved (or submitted) plan moves the visit on even if the AI never finished earlier steps,
    # e.g. the doctor took over mid-questionnaire and prescribed directly.
    if rx is not None and rx.status == "approved":
        return "booking" if s.ot_pending else "wrap_up"
    if rx is not None and rx.status == "pending_approval":
        return "approval"
    if s.assessment is None or not s.assessment.completed:
        return "assess"
    if missing_poses(s.photos):
        return "capture"
    if not any(a.kind == "diagnosis" for a in s.analyses):
        return "analyse"
    return "recommend"


async def load_state(db: AsyncSession, conversation_id: uuid.UUID, lock: bool = False) -> State:
    q = select(Conversation).where(Conversation.id == conversation_id)
    if lock:
        q = q.with_for_update()
    conv = (await db.execute(q)).scalar_one()
    s = State(conversation=conv)
    if conv.client_id:
        s.client = await db.get(Client, conv.client_id)
    if conv.visit_id:
        s.visit = await db.get(Visit, conv.visit_id)
        vid = conv.visit_id
        s.assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == vid))).scalar_one_or_none()
        s.photos = list((await db.execute(select(Photo).where(Photo.visit_id == vid).order_by(Photo.created_at))).scalars())
        s.analyses = list((await db.execute(select(Analysis).where(Analysis.visit_id == vid).order_by(Analysis.created_at))).scalars())
        s.prescription = (await db.execute(select(Prescription).where(Prescription.visit_id == vid))).scalar_one_or_none()
        s.approval = (await db.execute(select(Approval).where(Approval.visit_id == vid).order_by(Approval.created_at.desc()).limit(1))).scalar_one_or_none()
        if s.prescription is not None and s.prescription.status == "approved" and s.visit.status != "completed":
            from app.config import get_settings
            from app.services import ot

            if get_settings().ot_booking_enabled:
                s.ot_pending = ot.pending(await ot.plan_procedures(db, s.prescription), await ot.visit_bookings(db, vid))
    return s


async def refresh(db: AsyncSession, s: State) -> State:
    return await load_state(db, s.conversation.id)
