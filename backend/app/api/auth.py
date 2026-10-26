from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.integrations import platform_auth
from app.services.platform_users import staff_for
from app.models import User
from app.schemas import LoginIn, TokenOut, UserCreate, UserOut
from app.security import admin_only, create_token, current_user, hash_password, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    """The sign-in form. A clinic account's own password first; otherwise the person's WorkflowHub (platform) email and
    password, checked by the platform's auth service and matched to their clinic staff account by email (the same
    rules as automatic sign-in). The platform session then also serves the skin model gateway."""
    email = body.email.lower().strip()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None and user.is_active and verify_password(body.password, user.password_hash):
        return TokenOut(access_token=create_token(user), user=UserOut.model_validate(user))

    if get_settings().wfh_auth_mode == "none":
        raise HTTPException(401, "Incorrect email or password")
    try:
        pair = await platform_auth.password_login(email, body.password)
    except platform_auth.PlatformUnreachable:
        raise HTTPException(503, "Can't reach the WorkflowHub sign-in service just now. Clinic accounts still work.")
    info = await platform_auth.verify_platform_token(pair.access) if pair else None
    if not info:
        raise HTTPException(401, "Incorrect email or password")
    staff = await staff_for(db, info.get("email") or email, info.get("full_name") or info.get("name"), "login")
    platform_auth.log.info("[login] platform sign-in for user id=%s role=%s", staff.id, staff.role)
    await platform_auth.gateway_tokens.adopt_pair(pair, staff.full_name)
    return TokenOut(access_token=create_token(staff), user=UserOut.model_validate(staff))


@router.get("/auth/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user


@router.get("/admin/users", response_model=list[UserOut])
async def list_users(_: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(User).order_by(User.created_at))).scalars().all()


@router.post("/admin/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, _: User = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    if body.role == "doctor" and not body.registration_no:
        raise HTTPException(422, "Doctors need a medical registration number")
    exists = (await db.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "Email already registered")
    user = User(email=body.email.lower(), full_name=body.full_name, role=body.role,
                registration_no=body.registration_no, password_hash=hash_password(body.password))
    db.add(user)
    await db.commit()
    return user
