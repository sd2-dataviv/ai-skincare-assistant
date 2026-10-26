import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import Client, User

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_token(user: User) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.jwt_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    s = get_settings()
    try:
        payload = jwt.decode(creds.credentials, s.jwt_secret, algorithms=[s.jwt_algorithm])
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    if payload.get("role") == "client":  # a client's phone sign-in never opens staff endpoints
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Inactive user")
    return user


def require_roles(*roles: str):
    async def guard(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return guard


staff = require_roles("admin", "doctor", "operator")
doctor_only = require_roles("doctor")
doctor_or_admin = require_roles("doctor", "admin")
admin_only = require_roles("admin")


# ── Clients signing in on their own phone (mobile + OTP) ─────────────────────
CLIENT_TOKEN_DAYS = 7


def create_client_token(client: Client) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": str(client.id), "role": "client", "iat": int(now.timestamp()),
               "exp": int((now + timedelta(days=CLIENT_TOKEN_DAYS)).timestamp())}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


async def current_client(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Client:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    s = get_settings()
    try:
        payload = jwt.decode(creds.credentials, s.jwt_secret, algorithms=[s.jwt_algorithm])
        client_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in again")
    if payload.get("role") != "client":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This page is for clients")
    client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in again")
    return client
