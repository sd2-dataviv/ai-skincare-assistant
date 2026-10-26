"""Pictures made by the clinic's skin models (diagnosis overlays, post-treatment previews)

Revision ID: f2b6d81c47e9
Revises: e7a3f9c2d814
Create Date: 2026-10-26 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2b6d81c47e9"
down_revision: Union[str, None] = "e7a3f9c2d814"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_images",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("visit_id", sa.UUID(), nullable=False),
        sa.Column("photo_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("pose", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("storage_key", sa.String(length=300), nullable=True),
        sa.Column("content_type", sa.String(length=50), nullable=False),
        sa.Column("before_storage_key", sa.String(length=300), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_images_visit_id", "model_images", ["visit_id"])


def downgrade() -> None:
    op.drop_index("ix_model_images_visit_id", table_name="model_images")
    op.drop_table("model_images")
