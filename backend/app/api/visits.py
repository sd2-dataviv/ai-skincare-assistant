"""Visit endpoints mirroring the AI actions in spec §06, for staff and integrations."""
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import orchestrator
from app.api.common import audit, get_or_404, visit_detail
from app.db import get_db
from app.idempotency import run_idempotent
from app.integrations.storage import get_storage
from app.models import Assessment, Client, Photo, User, Visit
from app.schemas import (
    ApprovalDecisionIn,
    ApprovalOut,
    AssessmentIn,
    AssessmentOut,
    EscalateIn,
    EscalationOut,
    PhotoOut,
    PrescriptionIn,
    PrescriptionOut,
    ShareIn,
)
from app.security import doctor_only, doctor_or_admin, staff
from app.services import analysis as analysis_svc
from app.services import assessment as assessment_svc
from app.services import escalation as escalation_svc
from app.services import prescriptions as rx_svc
from app.services import summary as summary_svc
from app.services.photos import save_photo
from app.services.recommendations import recommend

router = APIRouter(tags=["visits"])


async def _visit(db: AsyncSession, visit_id: uuid.UUID) -> tuple[Visit, Client]:
    visit = await get_or_404(db, Visit, visit_id)
    client = await db.get(Client, visit.client_id)
    return visit, client


@router.get("/visits/{visit_id}")
async def get_visit(visit_id: uuid.UUID, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    visit = await get_or_404(db, Visit, visit_id)
    return await visit_detail(db, visit)


@router.put("/visits/{visit_id}/assessment", response_model=AssessmentOut)
async def put_assessment(visit_id: uuid.UUID, body: AssessmentIn, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    visit, client = await _visit(db, visit_id)
    a = await assessment_svc.save_answers(db, visit, client, body.answers)
    await audit(db, user, "save_assessment", visit.id, {"questions": list(body.answers)}, {"completed": a.completed}, "Staff saved answers")
    await escalation_svc.check_and_escalate(db, None, visit, client, a, [])
    await db.commit()
    return a


@router.post("/visits/{visit_id}/photos", response_model=PhotoOut, status_code=201)
async def upload_photo(visit_id: uuid.UUID, pose: str = Form(...), file: UploadFile = File(...),
                       user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    visit, _ = await _visit(db, visit_id)
    data = await file.read()
    photo = await save_photo(db, visit, pose, data, file.content_type or "")
    await audit(db, user, "upload_photo", visit.id, {"pose": pose, "bytes": len(data)}, {"accepted": photo.accepted}, "Staff upload")
    await db.commit()
    return photo


@router.get("/photos/{photo_id}/file")
async def photo_file(photo_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    photo = await get_or_404(db, Photo, photo_id)
    data = await get_storage().get(photo.storage_key)
    return Response(content=data, media_type=photo.content_type, headers={"Cache-Control": "no-store"})


@router.post("/photos/{photo_id}/analyses")
async def analyse_photo(photo_id: uuid.UUID, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    photo = await get_or_404(db, Photo, photo_id)
    visit, client = await _visit(db, photo.visit_id)
    photos = (await db.execute(select(Photo).where(Photo.visit_id == visit.id).order_by(Photo.created_at))).scalars().all()
    result = await analysis_svc.run_analysis(db, visit, client, list(photos))
    assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == visit.id))).scalar_one_or_none()
    await audit(db, user, "run_analysis", visit.id, {"photo_id": str(photo_id)}, {"confidence": result["confidence"]}, "Staff ran analysis")
    from app.models import Analysis
    analyses = (await db.execute(select(Analysis).where(Analysis.visit_id == visit.id))).scalars().all()
    await escalation_svc.check_and_escalate(db, None, visit, client, assessment, list(analyses))
    await db.commit()
    return result


@router.get("/visits/{visit_id}/recommendations")
async def get_recommendations(visit_id: uuid.UUID, _: User = Depends(doctor_or_admin), db: AsyncSession = Depends(get_db)):
    visit, client = await _visit(db, visit_id)
    from app.models import Analysis
    analyses = (await db.execute(select(Analysis).where(Analysis.visit_id == visit.id).order_by(Analysis.created_at))).scalars().all()
    diag = analysis_svc.latest(list(analyses), "diagnosis")
    if diag is None:
        from app.services import DomainError
        raise DomainError("no_analysis", "Run the analysis first.")
    assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == visit.id))).scalar_one_or_none()
    return await recommend(db, visit, client, assessment, diag.result["diagnosis"])


@router.put("/visits/{visit_id}/prescription", response_model=PrescriptionOut)
async def put_prescription(visit_id: uuid.UUID, body: PrescriptionIn, request: Request, user: User = Depends(staff),
                           db: AsyncSession = Depends(get_db)):
    visit, client = await _visit(db, visit_id)

    async def do():
        assessment = (await db.execute(select(Assessment).where(Assessment.visit_id == visit.id))).scalar_one_or_none()
        rx = await rx_svc.draft(db, visit, client, assessment, [i.model_dump() for i in body.items], body.rationale)
        await audit(db, user, "draft_prescription", visit.id, body.model_dump(), {"version": rx.version}, "Staff drafted plan")
        await db.commit()
        return PrescriptionOut.model_validate(rx)

    return await run_idempotent(db, request, user.id, do)


@router.post("/visits/{visit_id}/approval", response_model=ApprovalOut)
async def request_approval(visit_id: uuid.UUID, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    visit, _ = await _visit(db, visit_id)
    ap = await rx_svc.request_approval(db, visit)
    await audit(db, user, "request_approval", visit.id, {}, {"approval_id": str(ap.id)}, "Staff requested approval")
    await db.commit()
    return ap


@router.post("/visits/{visit_id}/approval/decision")
async def decide(visit_id: uuid.UUID, body: ApprovalDecisionIn, request: Request, doctor: User = Depends(doctor_only),
                 db: AsyncSession = Depends(get_db)):
    visit, _ = await _visit(db, visit_id)

    async def do():
        items = [i.model_dump() for i in body.items] if body.items else None
        ap = await rx_svc.decide(db, visit, doctor, body.decision, items, body.notes)
        await audit(db, doctor, f"approval_{body.decision}", visit.id, {"notes": body.notes}, {"status": ap.status},
                    "Doctor decision")
        await db.commit()
        messages = await orchestrator.on_approval_decided(db, visit, body.decision, doctor)
        return {"approval": ApprovalOut.model_validate(ap).model_dump(mode="json"), "conversation_messages": messages}

    return await run_idempotent(db, request, doctor.id, do)


@router.post("/visits/{visit_id}/escalate", response_model=EscalationOut, status_code=201)
async def escalate(visit_id: uuid.UUID, body: EscalateIn, user: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    visit, _ = await _visit(db, visit_id)
    conv = await orchestrator.latest_conversation_for_visit(db, visit.id)
    esc = await escalation_svc.escalate(db, conv, visit, "staff_requested", body.details)
    await audit(db, user, "escalate_to_doctor", visit.id, body.model_dump(), {"escalation_id": str(esc.id)}, "Staff escalated")
    await db.commit()
    return esc


@router.get("/visits/{visit_id}/summary")
async def get_summary(visit_id: uuid.UUID, _: User = Depends(staff), db: AsyncSession = Depends(get_db)):
    visit, _ = await _visit(db, visit_id)
    return await summary_svc.build_summary(db, visit)


@router.post("/visits/{visit_id}/summary/share")
async def share_summary(visit_id: uuid.UUID, body: ShareIn, request: Request, user: User = Depends(staff),
                        db: AsyncSession = Depends(get_db)):
    visit, _ = await _visit(db, visit_id)

    async def do():
        res = await summary_svc.share(db, visit, body.channel)
        await audit(db, user, "share_summary", visit.id, body.model_dump(), {"channel": res["channel"]}, "Staff shared summary")
        await db.commit()
        return res

    return await run_idempotent(db, request, user.id, do)
