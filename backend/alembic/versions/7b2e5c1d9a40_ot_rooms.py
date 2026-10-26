"""OT / procedure rooms: rooms, blocks, bookings; room type and duration on catalogue items

Revision ID: 7b2e5c1d9a40
Revises: 4954fc218f8a
Create Date: 2026-10-17 12:40:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7b2e5c1d9a40"
down_revision: Union[str, None] = "4954fc218f8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.add_column("catalogue_items", sa.Column("room_type", sa.String(length=30), nullable=True))
    op.add_column("catalogue_items", sa.Column("duration_min", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "ot_rooms",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.String(length=30)), nullable=False),
        sa.Column("open_time", sa.Time(), nullable=False),
        sa.Column("close_time", sa.Time(), nullable=False),
        sa.Column("open_days", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "ot_blocks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("room_id", sa.UUID(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["ot_rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ot_blocks_room_id", "ot_blocks", ["room_id"])
    op.create_table(
        "ot_bookings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("visit_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("catalogue_item_id", sa.String(length=60), nullable=False),
        sa.Column("session_index", sa.Integer(), nullable=False),
        sa.Column("sessions_total", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("room_id", sa.UUID(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clear_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["catalogue_item_id"], ["catalogue_items.id"]),
        sa.ForeignKeyConstraint(["room_id"], ["ot_rooms.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ot_bookings_visit_id", "ot_bookings", ["visit_id"])
    op.create_index("ix_ot_bookings_client_id", "ot_bookings", ["client_id"])
    op.create_index("ix_ot_bookings_room_id", "ot_bookings", ["room_id"])
    op.create_index("ix_ot_bookings_status", "ot_bookings", ["status"])
    op.create_index("uq_ot_booking_item_session", "ot_bookings", ["visit_id", "catalogue_item_id", "session_index"],
                    unique=True, postgresql_where=sa.text("status IN ('confirmed', 'deferred')"))
    op.execute("ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_overlap EXCLUDE USING gist "
               "(room_id WITH =, tstzrange(starts_at, clear_until) WITH &&) WHERE (status = 'confirmed')")


def downgrade() -> None:
    op.drop_table("ot_bookings")
    op.drop_index("ix_ot_blocks_room_id", table_name="ot_blocks")
    op.drop_table("ot_blocks")
    op.drop_table("ot_rooms")
    op.drop_column("catalogue_items", "duration_min")
    op.drop_column("catalogue_items", "room_type")
