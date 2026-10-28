"""Consultation services renamed to the clinic's names (Acne / Pigmentation / Age / Skin Scription, Anti Aging,
Medical Consultation; follow-ups from session 2). Visits keep their service under the new keys.

Revision ID: a9c4e1b7d205
Revises: f2b6d81c47e9
Create Date: 2026-10-28 14:30:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a9c4e1b7d205"
down_revision: Union[str, None] = "f2b6d81c47e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE visits SET programme = 'age' WHERE programme = 'anti_ageing'")
    op.execute("UPDATE visits SET programme = 'skin' WHERE programme IN ('glow_hydration', 'general')")


def downgrade() -> None:
    # Skin Scription covered both old programmes; the old split can't be recovered, so they go back as the general one.
    op.execute("UPDATE visits SET programme = 'anti_ageing' WHERE programme = 'age'")
    op.execute("UPDATE visits SET programme = 'general' WHERE programme IN ('skin', 'anti_aging', 'medical')")
