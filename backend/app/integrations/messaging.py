"""Outbound OTP / SMS / WhatsApp. The mock keeps an in-memory outbox for tests and never logs message bodies."""
import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.config import get_settings

log = logging.getLogger(__name__)

MOCK_OTP_CODE = "123456"


@dataclass
class Outbox:
    sent: list[dict] = field(default_factory=list)


OUTBOX = Outbox()


class Messenger(Protocol):
    async def send(self, channel: str, to: str, body: str) -> str: ...


class MockMessenger:
    async def send(self, channel: str, to: str, body: str) -> str:
        OUTBOX.sent.append({"channel": channel, "to": to, "body": body})
        log.info("mock %s message queued", channel)
        return "sent"


def get_messenger() -> Messenger:
    # A real provider (e.g. Twilio, Gupshup, MSG91) plugs in here with the same interface.
    return MockMessenger()


def generate_otp() -> str:
    import secrets

    if get_settings().otp_mode == "mock":
        return MOCK_OTP_CODE
    return f"{secrets.randbelow(10**6):06d}"
