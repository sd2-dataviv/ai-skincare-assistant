"""The talking avatar on the clinic device (app/integrations/avatar.py; docs/09-avatar.md).

1. `POST /api/avatar/session` (operator or admin, as for the consultation screen) answers a ticket that lasts 60 s and
   works once. Browsers can't set headers on a WebSocket, and the staff session token stays out of URLs and logs.
2. `WS /api/avatar/ws?ticket=...` relays between the browser and the avatar service in Direct mode.
3. `POST /api/avatar/speech` is voice-only mode: one reply as speech (WAV) in the avatar's voice, for when the avatar
   itself can't be used. The browser plays it behind a voice orb.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket
from pydantic import BaseModel, Field

from app.config import get_settings
from app.integrations import avatar
from app.models import User
from app.security import require_roles

router = APIRouter(tags=["avatar"])
log = logging.getLogger("skincare.avatar")

AUDIENCE = "avatar-ws"
TICKET_TTL_S = 60
CLOSE_BAD_TICKET = 4401
_used: dict[str, float] = {}  # ticket id -> expiry; a ticket opens one socket (the API runs as one worker)

kiosk = require_roles("operator", "admin")


@router.post("/avatar/session")
async def avatar_session(user: User = Depends(kiosk)):
    s = get_settings()
    if not avatar.enabled():
        raise HTTPException(404, "The talking avatar is not set up")
    now = datetime.now(timezone.utc)
    ticket = jwt.encode({"sub": str(user.id), "who": user.email, "aud": AUDIENCE, "jti": uuid.uuid4().hex,
                         "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=TICKET_TTL_S)).timestamp())},
                        s.jwt_secret, algorithm=s.jwt_algorithm)
    return {"ticket": ticket, "avatar_id": s.avatar_id, "language": s.avatar_language, "path": "/api/avatar/ws"}


class SpeechIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/avatar/speech")
async def avatar_speech(body: SpeechIn, _: User = Depends(kiosk)):
    """Voice-only mode. 404 when the avatar isn't set up; 503 when its speech engine can't be used (the browser then
    uses its own voice). The reply's text is never logged."""
    if not avatar.tts_url():
        raise HTTPException(404, "Voice-only mode is not set up")
    try:
        wav = await avatar.synthesise(body.text)
    except avatar.AvatarUnavailable as e:
        log.warning("[avatar] voice-only speech failed: %s", e.message)
        raise HTTPException(503, e.message)
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


def _check_ticket(ticket: str | None) -> dict | None:
    s = get_settings()
    if not ticket:
        return None
    try:
        claims = jwt.decode(ticket, s.jwt_secret, algorithms=[s.jwt_algorithm], audience=AUDIENCE,
                            options={"require": ["exp", "jti", "sub"]})
    except jwt.PyJWTError:
        return None
    now = time.time()
    for k in [k for k, exp in _used.items() if exp < now]:
        del _used[k]
    if claims["jti"] in _used:
        return None
    _used[claims["jti"]] = float(claims["exp"])
    return claims


@router.websocket("/avatar/ws")
async def avatar_socket(ws: WebSocket, ticket: str | None = None):
    await ws.accept()
    claims = _check_ticket(ticket)
    if claims is None:
        await ws.close(code=CLOSE_BAD_TICKET, reason="Ticket missing, expired or used")
        return
    if not avatar.enabled():
        await ws.close(code=avatar.CLOSE_UNAVAILABLE, reason="The talking avatar is not set up")
        return
    s = get_settings()
    await avatar.Relay(client=ws, who=str(claims.get("who") or claims["sub"]), avatar_id=s.avatar_id,
                       language=s.avatar_language).run()
