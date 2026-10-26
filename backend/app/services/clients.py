import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.d365 import get_d365
from app.models import Client
from app.services import DomainError


def normalise_mobile(raw: str) -> str:
    """Return an E.164 Indian mobile number (+91XXXXXXXXXX) or raise."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise DomainError("invalid_mobile", "That doesn't look like a valid 10-digit Indian mobile number.", 422)
    return f"+91{digits}"


def extract_mobile(text: str) -> str | None:
    for m in re.finditer(r"(\+?\d[\d\s-]{8,15}\d)", text or ""):
        try:
            return normalise_mobile(m.group(1))
        except DomainError:
            continue
    return None


def mask_mobile(mobile: str) -> str:
    return f"••••••{mobile[-4:]}" if mobile else ""


def age_of(client: Client, today: date | None = None) -> int | None:
    if not client.dob:
        return None
    today = today or date.today()
    return today.year - client.dob.year - ((today.month, today.day) < (client.dob.month, client.dob.day))


async def get_by_mobile(db: AsyncSession, mobile: str) -> Client | None:
    return (await db.execute(select(Client).where(Client.mobile == mobile))).scalar_one_or_none()


async def find_or_import(
    db: AsyncSession,
    mobile: str,
    full_name: str | None = None,
    dob: date | None = None,
    gender: str | None = None,
) -> tuple[Client | None, str]:
    """Find locally, else import from D365, else create if a profile is supplied.

    Returns (client, how) where how is found | imported | created | needs_profile.
    """
    mobile = normalise_mobile(mobile)
    client = await get_by_mobile(db, mobile)
    if client:
        if client.dob is None and dob is not None:
            client.dob = dob
            await db.flush()
        return client, "found"
    contact = await get_d365().find_by_mobile(mobile)
    if contact:
        client = Client(
            mobile=mobile, full_name=contact.full_name, dob=contact.dob or dob, gender=contact.gender,
            email=contact.email, external_ref=contact.external_ref, source="d365",
        )
        db.add(client)
        await db.flush()
        return client, "imported"
    if not full_name:
        return None, "needs_profile"
    if gender and gender not in ("female", "male", "other"):
        raise DomainError("invalid_gender", "Gender must be female, male or other.", 422)
    client = Client(mobile=mobile, full_name=full_name.strip()[:200], dob=dob, gender=gender, source="ai")
    db.add(client)
    await db.flush()
    return client, "created"
