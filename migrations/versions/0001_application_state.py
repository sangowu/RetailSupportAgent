"""M1: application state (sessions, messages, confirmations, write operations, audit).

Revision ID: 0001_application_state
Revises:
Create Date: 2026-09-08

Application-side tables only. The simulated order data of tau2-bench stays out of the
database: it is the benchmark's business data, and moving it here would amount to
rewriting the official evaluation expectations (see docs/M1_PERSISTENCE.md section 1).

`downgrade` really runs. A migration that only goes up leaves no way back when the first
deployment fails.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_application_state"
down_revision = None
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("tool_calls", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Resending the same turn cannot insert a second row: the unique constraint
        # guarantees it, not an application-level "check whether it exists"
        sa.UniqueConstraint("session_id", "seq", name="uq_messages_session_seq"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])

    op.create_table(
        "confirmations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state in ('pending', 'confirmed', 'cancelled')", name="ck_confirmations_state"
        ),
    )
    op.create_index("ix_confirmations_fingerprint", "confirmations", ["fingerprint"])
    # Partial unique index: at most one pending confirmation per session at a time. It
    # applies only to pending rows; otherwise a session could ever hold just one
    # confirmation.
    op.create_index(
        "uq_confirmations_one_pending_per_session",
        "confirmations",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )

    op.create_table(
        "operations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "confirmation_id",
            sa.Integer,
            sa.ForeignKey("confirmations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("result", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="succeeded"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Idempotency rests on this: a second insert of the same key conflicts, and the
        # conflict means "already executed"
        sa.UniqueConstraint("idempotency_key", name="uq_operations_idempotency_key"),
        sa.CheckConstraint("status in ('succeeded', 'failed')", name="ck_operations_status"),
    )
    op.create_index("ix_operations_session_id", "operations", ["session_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operation_id",
            sa.Integer,
            sa.ForeignKey("operations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_events_session_id", "audit_events", ["session_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("operations")
    op.drop_table("confirmations")
    op.drop_table("messages")
    op.drop_table("sessions")
