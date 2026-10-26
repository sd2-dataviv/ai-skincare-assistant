import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# Auth
class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(ORM):
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    registration_no: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserCreate(BaseModel):
    email: str
    full_name: str
    role: Literal["admin", "doctor", "operator"]
    password: str = Field(min_length=10)
    registration_no: str | None = None


# Clients & visits
class ClientOut(ORM):
    id: uuid.UUID
    mobile: str
    full_name: str
    dob: date | None
    gender: str | None
    source: str
    external_ref: str | None


class ClientImportIn(BaseModel):
    mobile: str


class ClientCreateIn(BaseModel):
    mobile: str
    full_name: str = Field(min_length=2, max_length=200)
    dob: date | None = None
    gender: Literal["female", "male", "other"] | None = None


class VisitCreateIn(BaseModel):
    concern_text: str = Field(min_length=2, max_length=2000)


class VisitOut(ORM):
    id: uuid.UUID
    client_id: uuid.UUID
    programme: str
    session_number: int
    status: str
    concern_text: str | None
    concerns: list[str]
    next_session_date: date | None
    created_at: datetime
    completed_at: datetime | None


class AssessmentIn(BaseModel):
    answers: dict[str, str]


class AssessmentOut(ORM):
    visit_id: uuid.UUID
    answers: dict
    skipped: dict
    skin_type: str | None
    sensitive: bool
    flags: dict
    completed: bool


class PhotoOut(ORM):
    id: uuid.UUID
    visit_id: uuid.UUID
    pose: str
    width: int
    height: int
    quality: dict
    accepted: bool
    created_at: datetime


class AnalysisOut(ORM):
    id: uuid.UUID
    visit_id: uuid.UUID
    photo_id: uuid.UUID | None
    kind: str
    engine: str
    result: dict
    confidence: float
    created_at: datetime


class RxItemIn(BaseModel):
    catalogue_id: str
    sessions: int | None = Field(default=None, ge=1, le=24)
    instructions: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=500)


class PrescriptionIn(BaseModel):
    items: list[RxItemIn] = Field(min_length=1, max_length=20)
    rationale: str | None = Field(default=None, max_length=2000)


class PrescriptionOut(ORM):
    id: uuid.UUID
    visit_id: uuid.UUID
    status: str
    items: list
    safety: dict
    rationale: str | None
    version: int
    updated_at: datetime


class ApprovalDecisionIn(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    items: list[RxItemIn] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ApprovalOut(ORM):
    id: uuid.UUID
    visit_id: uuid.UUID
    prescription_version: int
    doctor_id: uuid.UUID | None
    status: str
    notes: str | None
    created_at: datetime
    decided_at: datetime | None


class EscalateIn(BaseModel):
    details: str = Field(min_length=2, max_length=1000)


class EscalationOut(ORM):
    id: uuid.UUID
    visit_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    trigger: str
    details: str | None
    assigned_doctor_id: uuid.UUID | None
    status: str
    created_at: datetime
    resolved_at: datetime | None


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    tags: list[str] = []
    visit_id: uuid.UUID | None = None


class NoteOut(ORM):
    id: uuid.UUID
    client_id: uuid.UUID
    visit_id: uuid.UUID | None
    author_type: str
    text: str
    tags: list[str]
    created_at: datetime


class ShareIn(BaseModel):
    channel: Literal["sms", "whatsapp", "link"] = "sms"


class CatalogueOut(ORM):
    id: str
    kind: str
    name: str
    description: str
    concerns: list[str]
    skin_types: list[str]
    contraindications: list[str]
    ingredients: list[str]
    default_sessions: int
    interval_days: int
    usage: str
    price_inr: int
    room_type: str | None = None
    duration_min: int = 0


class ActionOut(ORM):
    id: uuid.UUID
    action: str
    actor: str
    inputs: dict
    result: dict
    reason: str
    status: str
    stage: str | None
    created_at: datetime


# Conversations
class ConversationCreateIn(BaseModel):
    consent_ai: bool
    consent_recording: bool
    language: str = Field(default="en-IN", max_length=10)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=8, max_length=64)


class DoctorMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ResolveIn(BaseModel):
    hand_back_to_ai: bool = True





# ── Follow-up chats ──────────────────────────────────────────────────────────
class ThreadOpenIn(BaseModel):
    visit_id: uuid.UUID | None = None  # None = the client's general chat


class StaffThreadOpenIn(BaseModel):
    client_id: uuid.UUID
    visit_id: uuid.UUID | None = None


class ThreadMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    client_message_id: str | None = Field(default=None, max_length=64)


class ClientAuthStartIn(BaseModel):
    mobile: str = Field(min_length=6, max_length=20)


class ClientAuthVerifyIn(BaseModel):
    mobile: str = Field(min_length=6, max_length=20)
    code: str = Field(min_length=4, max_length=8)
