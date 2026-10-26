"""Which clinic staff account a WorkflowHub platform user is. One set of rules for every way a platform identity
arrives: automatic sign-in (SSO), the login form with platform email + password, and calls from other apps and AI
agents (X-Wfh-User).

- matched by email (platform accounts and clinic accounts share it);
- no clinic account: an operator account is created (WFH_DEFAULT_ROLE, default "operator"; empty refuses them).
  Doctors and admins are never created this way: they sign in with their clinic accounts;
- a disabled clinic account outranks a valid platform identity;
- platform admin is NOT clinic admin.
"""
import logging
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User
from app.security import hash_password

log = logging.getLogger("skincare.sso")
PROVISIONABLE = {"operator"}


async def staff_for(db: AsyncSession, email: str | None, name: str | None, how: str) -> User:
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(status.HTTP_409_CONFLICT, "Platform account has no email address")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        role = (get_settings().wfh_default_role or "").strip().lower()
        if role not in PROVISIONABLE:
            log.info("[%s] refused: no clinic staff account for this platform user", how)
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"There is no clinic staff account for {email}. Ask a clinic admin to add one.")
        # Unusable random password: reachable only through the platform (or an admin reset).
        user = User(email=email, full_name=(name or email.split("@")[0]).strip()[:200], role=role,
                    password_hash=hash_password(secrets.token_hex(24)))
        db.add(user)
        await db.commit()
        await db.refresh(user)
        log.info("[%s] created clinic account id=%s role=%s", how, user.id, user.role)
    elif not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This clinic account is disabled")
    return user
