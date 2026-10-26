import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.integrations.messaging import generate_otp, get_messenger
from app.models import OtpChallenge
from app.services import DomainError


def _hash(mobile: str, code: str) -> str:
    key = get_settings().jwt_secret.encode()
    return hmac.new(key, f"{mobile}:{code}".encode(), hashlib.sha256).hexdigest()


async def send_otp(db: AsyncSession, mobile: str) -> dict:
    s = get_settings()
    now = datetime.now(timezone.utc)
    recent = await db.scalar(
        select(func.count()).select_from(OtpChallenge).where(
            OtpChallenge.mobile == mobile, OtpChallenge.created_at > now - timedelta(minutes=10)
        )
    )
    if (recent or 0) >= s.otp_max_sends_per_10min:
        raise DomainError("otp_rate_limited", "Too many codes requested. Please wait a few minutes.", 429)
    code = generate_otp()
    db.add(OtpChallenge(mobile=mobile, code_hash=_hash(mobile, code), expires_at=now + timedelta(seconds=s.otp_ttl_seconds)))
    await db.flush()
    await get_messenger().send("sms", mobile, f"Your skin consultation code is {code}. It expires in 5 minutes.")
    return {"sent": True, "expires_in_s": s.otp_ttl_seconds, "mock": s.otp_mode == "mock"}


async def verify_otp(db: AsyncSession, mobile: str, code: str) -> bool:
    s = get_settings()
    now = datetime.now(timezone.utc)
    ch = (
        await db.execute(
            select(OtpChallenge)
            .where(OtpChallenge.mobile == mobile, OtpChallenge.verified_at.is_(None))
            .order_by(OtpChallenge.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if ch is None or ch.expires_at < now:
        raise DomainError("otp_expired", "That code has expired. I can send a new one.", 410)
    if ch.attempts >= s.otp_max_attempts:
        raise DomainError("otp_locked", "Too many wrong attempts. Please request a new code.", 429)
    ch.attempts += 1
    ok = hmac.compare_digest(ch.code_hash, _hash(mobile, code.strip()))
    if ok:
        ch.verified_at = now
    await db.flush()
    return ok
