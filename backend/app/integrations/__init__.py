"""Adapters for external systems. Each module exposes an interface, a mock and a factory driven by settings."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.config import get_settings

T = TypeVar("T")
log = logging.getLogger(__name__)


async def with_retries(fn: Callable[[], Awaitable[T]], what: str) -> T:
    """Bounded retries with backoff (NFR-07: retries are limited)."""
    attempts = get_settings().integration_max_retries
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("%s failed (attempt %d/%d): %s", what, i + 1, attempts, type(e).__name__)
            await asyncio.sleep(0.3 * (2**i))
    assert last is not None
    raise last
