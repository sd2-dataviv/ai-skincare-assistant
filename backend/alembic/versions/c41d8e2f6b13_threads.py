"""Follow-up chats: a thread per client and past visit (or general), with its messages

Revision ID: c41d8e2f6b13
Revises: 7b2e5c1d9a40
Create Date: 2026-10-20 12:10:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c41d8e2f6b13"
down_revision: Union[str, None] = "7b2e5c1d9a40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("visit_id", sa.UUID(), nullable=True),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handover_reason", sa.String(length=40), nullable=True),
        sa.Column("handover_details", sa.Text(), nullable=True),
        sa.Column("assigned_user_id", sa.UUID(), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_read_seq", sa.Integer(), nullable=False),
        sa.Column("staff_read_seq", sa.Integer(), nullable=False),
        sa.Column("client_notified_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_threads_client_id", "threads", ["client_id"])
    op.create_index("ix_threads_waiting_since", "threads", ["waiting_since"])
    op.create_index("uq_thread_client_visit", "threads", ["client_id", "visit_id"], unique=True,
                    postgresql_where=sa.text("visit_id IS NOT NULL"))
    op.create_index("uq_thread_client_general", "threads", ["client_id"], unique=True,
                    postgresql_where=sa.text("visit_id IS NULL"))

    op.create_table(
        "thread_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=True),
        sa.Column("client_message_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", "seq", name="uq_thread_message_seq"),
    )
    op.create_index("ix_thread_messages_thread_id", "thread_messages", ["thread_id"])
    op.create_index("uq_thread_message_cmid", "thread_messages", ["thread_id", "client_message_id"], unique=True,
                    postgresql_where=sa.text("client_message_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_table("thread_messages")
    op.drop_table("threads")
