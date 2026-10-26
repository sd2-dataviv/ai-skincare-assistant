"""Idempotency-Key support (NFR-07): a repeated submission returns the first response instead of acting twice.

The key is claimed (inserted) *before* the work runs, so two concurrent requests with the same key can't both act.
"""
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyKey

IN_PROGRESS = 0


async def _existing(db: AsyncSession, key: str, scope: str) -> Any:
    row = (await db.execute(select(IdempotencyKey).where(IdempotencyKey.key == key)
                            .execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        return None
    if row.scope != scope:
        raise HTTPException(422, "Idempotency-Key reused for a different request")
    if row.status_code == IN_PROGRESS:
        raise HTTPException(409, "The same request is already being processed")
    return row.response


async def run_idempotent(
    db: AsyncSession,
    request: Request,
    user_id: Any,
    fn: Callable[[], Awaitable[Any]],
    status_code: int = 200,
    check_body: bool = False,
) -> Any:
    key = request.headers.get("Idempotency-Key")
    if not key:
        return await fn()
    if len(key) > 100:
        raise HTTPException(400, "Idempotency-Key too long")
    scope = f"{request.method} {request.url.path} {user_id}"
    if check_body:  # the same key sent with different details is refused instead of replayed
        scope += " " + hashlib.sha256(await request.body()).hexdigest()[:16]
    prior = await _existing(db, key, scope)
    if prior is not None:
        return prior
    claimed = (await db.execute(
        insert(IdempotencyKey).values(key=key, scope=scope, status_code=IN_PROGRESS, response={})
        .on_conflict_do_nothing(index_elements=["key"]).returning(IdempotencyKey.key)
    )).scalar_one_or_none()
    await db.commit()
    if claimed is None:  # lost the race to a concurrent request
        prior = await _existing(db, key, scope)
        return prior
    try:
        result = await fn()
    except Exception:
        await db.rollback()
        await db.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
        await db.commit()
        raise
    body = jsonable_encoder(result)
    await db.execute(update(IdempotencyKey).where(IdempotencyKey.key == key).values(status_code=status_code, response=body))
    await db.commit()
    return body
