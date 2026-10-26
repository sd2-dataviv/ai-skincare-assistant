from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.schemas import LoginIn, TokenOut, UserCreate, UserOut
from app.security import admin_only, create_token, current_user, hash_password, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == body.email.lower().strip()))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    return TokenOut(access_token=create_token(user), user=UserOut.model_validate(user))


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
