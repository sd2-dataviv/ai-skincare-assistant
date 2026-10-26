"""OT resources: equipment, doctor hours, holidays, tentative bookings, readiness checklist, day-of stages, versions

Revision ID: e7a3f9c2d814
Revises: c41d8e2f6b13
Create Date: 2026-10-21 11:30:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7a3f9c2d814"
down_revision: Union[str, None] = "c41d8e2f6b13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HOLDING = "status IN ('tentative', 'confirmed')"


def upgrade() -> None:
    op.add_column("catalogue_items", sa.Column("equipment_type", sa.String(length=40), nullable=True))
    op.add_column("catalogue_items", sa.Column("needs_doctor", sa.Boolean(), nullable=False, server_default="false"))

    op.create_table(
        "ot_equipment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("equipment_type", sa.String(length=40), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_ot_equipment_equipment_type", "ot_equipment", ["equipment_type"])

    op.create_table(
        "doctor_hours",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_doctor_hours_user_id", "doctor_hours", ["user_id"])

    op.create_table(
        "clinic_holidays",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )

    # Blocks: a room, a doctor (leave) or a machine (maintenance)
    op.alter_column("ot_blocks", "room_id", nullable=True)
    op.add_column("ot_blocks", sa.Column("user_id", sa.UUID(), nullable=True))
    op.add_column("ot_blocks", sa.Column("equipment_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_ot_blocks_user", "ot_blocks", "users", ["user_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_ot_blocks_equipment", "ot_blocks", "ot_equipment", ["equipment_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_ot_blocks_user_id", "ot_blocks", ["user_id"])
    op.create_index("ix_ot_blocks_equipment_id", "ot_blocks", ["equipment_id"])
    op.create_check_constraint("ck_ot_block_one_target", "ot_blocks", "num_nonnulls(room_id, user_id, equipment_id) = 1")

    # Bookings
    for col in (
        sa.Column("doctor_id", sa.UUID(), nullable=True),
        sa.Column("equipment_id", sa.UUID(), nullable=True),
        sa.Column("equipment_clear_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("stage", sa.String(length=12), nullable=False, server_default="scheduled"),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("override_reason", sa.String(length=300), nullable=True),
        sa.Column("overridden_rules", postgresql.ARRAY(sa.String(length=40)), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    ):
        op.add_column("ot_bookings", col)
    op.create_foreign_key("fk_ot_bookings_doctor", "ot_bookings", "users", ["doctor_id"], ["id"])
    op.create_foreign_key("fk_ot_bookings_equipment", "ot_bookings", "ot_equipment", ["equipment_id"], ["id"])
    op.create_foreign_key("fk_ot_bookings_confirmed_by", "ot_bookings", "users", ["confirmed_by_user_id"], ["id"])
    op.create_index("ix_ot_bookings_doctor_id", "ot_bookings", ["doctor_id"])
    op.create_index("ix_ot_bookings_equipment_id", "ot_bookings", ["equipment_id"])
    # Bookings made before this change were firm: mark them confirmed at the time they were made.
    op.execute("UPDATE ot_bookings SET confirmed_at = created_at WHERE status = 'confirmed'")

    # Tentative bookings hold resources too
    op.execute("ALTER TABLE ot_bookings DROP CONSTRAINT IF EXISTS ex_ot_booking_overlap")
    op.execute("ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_overlap EXCLUDE USING gist "
               f"(room_id WITH =, tstzrange(starts_at, clear_until) WITH &&) WHERE ({HOLDING})")
    op.execute("ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_doctor_overlap EXCLUDE USING gist "
               f"(doctor_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE ({HOLDING} AND doctor_id IS NOT NULL)")
    op.execute("ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_equipment_overlap EXCLUDE USING gist "
               "(equipment_id WITH =, tstzrange(starts_at, equipment_clear_until) WITH &&) "
               f"WHERE ({HOLDING} AND equipment_id IS NOT NULL)")
    op.drop_index("uq_ot_booking_item_session", table_name="ot_bookings")
    op.create_index("uq_ot_booking_item_session", "ot_bookings", ["visit_id", "catalogue_item_id", "session_index"],
                    unique=True, postgresql_where=sa.text("status IN ('tentative', 'confirmed', 'deferred')"))

    op.create_table(
        "ot_readiness_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("booking_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("mandatory", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["ot_bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("booking_id", "key", name="uq_ot_readiness_booking_key"),
    )
    op.create_index("ix_ot_readiness_items_booking_id", "ot_readiness_items", ["booking_id"])


def downgrade() -> None:
    op.drop_table("ot_readiness_items")
    op.drop_index("uq_ot_booking_item_session", table_name="ot_bookings")
    op.create_index("uq_ot_booking_item_session", "ot_bookings", ["visit_id", "catalogue_item_id", "session_index"],
                    unique=True, postgresql_where=sa.text("status IN ('confirmed', 'deferred')"))
    op.execute("ALTER TABLE ot_bookings DROP CONSTRAINT IF EXISTS ex_ot_booking_equipment_overlap")
    op.execute("ALTER TABLE ot_bookings DROP CONSTRAINT IF EXISTS ex_ot_booking_doctor_overlap")
    op.execute("ALTER TABLE ot_bookings DROP CONSTRAINT IF EXISTS ex_ot_booking_overlap")
    op.execute("UPDATE ot_bookings SET status = 'confirmed' WHERE status = 'tentative'")
    op.execute("ALTER TABLE ot_bookings ADD CONSTRAINT ex_ot_booking_overlap EXCLUDE USING gist "
               "(room_id WITH =, tstzrange(starts_at, clear_until) WITH &&) WHERE (status = 'confirmed')")
    for c in ("version", "overridden_rules", "override_reason", "finished_at", "started_at", "checked_in_at", "stage",
              "confirmed_by_user_id", "confirmed_at", "equipment_clear_until", "equipment_id", "doctor_id"):
        op.drop_column("ot_bookings", c)
    op.drop_constraint("ck_ot_block_one_target", "ot_blocks")
    op.execute("DELETE FROM ot_blocks WHERE room_id IS NULL")
    op.drop_column("ot_blocks", "equipment_id")
    op.drop_column("ot_blocks", "user_id")
    op.alter_column("ot_blocks", "room_id", nullable=False)
    op.drop_table("clinic_holidays")
    op.drop_table("doctor_hours")
    op.drop_table("ot_equipment")
    op.drop_column("catalogue_items", "needs_doctor")
    op.drop_column("catalogue_items", "equipment_type")
