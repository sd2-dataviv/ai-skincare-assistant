"""Platform SSO: exchange a WorkflowHub platform token for a clinic staff session.

POST /api/auth/platform-session is the password-less twin of POST /api/auth/login. Instead of an email and password
it takes the platform token the user already holds, verifies it server-side against the same auth service the
platform uses (WFH_AUTH_SERVICE_URL, injected by WorkflowHub), and returns exactly what /login returns: the same
create_token(), the same TokenOut. Nothing downstream can tell the two sessions apart; the login form keeps working.

A separate endpoint rather than a branch in /login: the handshake fires automatically on page load, so it must not
be counted or audited as a password attempt.

TRUST: the browser relays a token but never asserts who it is; the identity comes only from the auth service's
answer. Every failure (forged, expired, auth service down) is a 401, and the user lands on the normal login form.

Clinic-specific rules (health data):
- a platform account is matched to a clinic staff account by email;
- a platform user with no clinic account becomes an operator (WFH_DEFAULT_ROLE; empty refuses them); doctors and
  admins are never created this way and sign in with their clinic accounts;
- a disabled clinic account outranks a valid platform session;
- platform admin is NOT clinic admin.

After a successful sign-in the platform session is also handed over for the skin model gateway (see
app/integrations/platform_auth.py), unless a service account is configured.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.integrations import platform_auth
from app.schemas import TokenOut, UserOut
from app.models import User
from app.security import create_token, staff
from app.services.platform_users import staff_for

router = APIRouter(prefix="/auth", tags=["platform SSO"])
log = platform_auth.log


class PlatformSessionIn(BaseModel):
    token: str = Field(min_length=10, max_length=8192)


@router.post("/platform-session", response_model=TokenOut)
async def platform_session(body: PlatformSessionIn, db: AsyncSession = Depends(get_db)):
    if get_settings().wfh_auth_mode == "none":
        raise HTTPException(404, "Platform sign-in is not enabled")
    info = await platform_auth.verify_platform_token(body.token)
    if not info:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired platform session")
    user = await staff_for(db, info.get("email"), info.get("full_name") or info.get("name"), "SSO")
    log.info("[SSO] session issued for user id=%s role=%s", user.id, user.role)
    # The skin model gateway needs a platform token too; keep this user's session for it (no-op with a service account).
    await platform_auth.gateway_tokens.adopt_sso(body.token, user.full_name)
    return TokenOut(access_token=create_token(user), user=UserOut.model_validate(user))


@router.post("/platform-token")
async def platform_token(body: PlatformSessionIn, user: User = Depends(staff)):
    """The open web app passes on the person's CURRENT platform token (WorkflowHub renews it) so the skin models keep
    a valid token when the platform won't hand this app a session of its own (handoff is admin-only). Ignored when
    a service account or a self-renewing session is in use. Checked with the auth service like any platform token."""
    info = await platform_auth.verify_platform_token(body.token)
    if not info:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired platform session")
    used = await platform_auth.gateway_tokens.adopt_access(body.token, info.get("full_name") or user.full_name)
    return {"used_for_models": used}


@router.get("/platform-config")
async def platform_config():
    """What the web app needs before it has a session (no sign-in by necessity). {"sso_enabled": true} is also what
    switches WorkflowHub's sign-in gate on for the app."""
    mode = get_settings().wfh_auth_mode
    return {"sso_enabled": mode != "none", "auth_mode": mode}
