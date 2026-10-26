"""Dynamics 365 contact lookup. Mock returns a few sample contacts; HTTP mode calls a configured gateway."""
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import httpx

from app.config import get_settings
from app.integrations import with_retries


@dataclass
class D365Contact:
    external_ref: str
    full_name: str
    mobile: str
    dob: date | None
    gender: str | None
    email: str | None


class D365Client(Protocol):
    async def find_by_mobile(self, mobile: str) -> D365Contact | None: ...


_SAMPLE = {
    "+919876543210": D365Contact("D365-000184", "Priya Sharma", "+919876543210", date(1992, 4, 11), "female", None),
    "+919811122233": D365Contact("D365-000231", "Rohan Mehta", "+919811122233", date(1988, 9, 2), "male", None),
    "+919900011122": D365Contact("D365-000377", "Ananya Gupta", "+919900011122", date(2010, 1, 20), "female", None),
}


class MockD365:
    async def find_by_mobile(self, mobile: str) -> D365Contact | None:
        return _SAMPLE.get(mobile)


class HttpD365:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def find_by_mobile(self, mobile: str) -> D365Contact | None:
        async def call():
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.base_url}/contacts", params={"mobile": mobile})
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                d = r.json()
                return D365Contact(
                    d["id"], d["fullName"], mobile,
                    date.fromisoformat(d["birthDate"]) if d.get("birthDate") else None,
                    d.get("gender"), d.get("email"),
                )

        return await with_retries(call, "d365.find_by_mobile")


def get_d365() -> D365Client:
    s = get_settings()
    if s.d365_mode == "http" and s.d365_base_url:
        return HttpD365(s.d365_base_url)
    return MockD365()
