"""M4: tickets, assignments and approvals.

Revision ID: 0004_tickets
Revises: 0003_identities
Create Date: 2026-09-08

`sessions.ticket_id` has no foreign key on purpose: `tickets.session_id` already points
the other way, and closing the cycle with a second constraint would make either row
impossible to insert first.

`downgrade` really runs, for the same reason as 0001 through 0003.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

JSONB = postgresql.JSONB(astext_type=sa.Text())

revision = "0004_tickets"
down_revision = "0003_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(64), nullable=False),
        # NULL where authentication is off and there is no subject to record.
        sa.Column("requested_by", sa.String(128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("resolution", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state in ('open', 'assigned', 'resolved', 'rejected')", name="ck_tickets_state"
        ),
    )
    op.create_index("ix_tickets_session_id", "tickets", ["session_id"])
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])
    # Partial unique index: one live ticket per session, so "is this session waiting for
    # a human" has exactly one answer.
    op.create_index(
        "uq_tickets_one_open_per_session",
        "tickets",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("state in ('open', 'assigned')"),
    )

    op.create_table(
        "ticket_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id",
            sa.Integer(),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assignee", sa.String(128), nullable=False),
        sa.Column("assigned_by", sa.String(128), nullable=False),
        sa.Column(
            "assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ticket_assignments_ticket_id", "ticket_assignments", ["ticket_id"])
    op.create_index("ix_ticket_assignments_assignee", "ticket_assignments", ["assignee"])
    op.create_index(
        "uq_ticket_assignments_one_active",
        "ticket_assignments",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text("released_at is null"),
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id",
            sa.Integer(),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # No default: an authorization with no end is one that can be used months later.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state in ('pending', 'approved', 'rejected')", name="ck_approvals_state"
        ),
    )
    op.create_index("ix_approvals_ticket_id", "approvals", ["ticket_id"])
    op.create_index("ix_approvals_session_id", "approvals", ["session_id"])
    op.create_index("ix_approvals_fingerprint", "approvals", ["fingerprint"])

    op.add_column("sessions", sa.Column("ticket_id", sa.Integer(), nullable=True))
    op.create_index("ix_sessions_ticket_id", "sessions", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_ticket_id", table_name="sessions")
    op.drop_column("sessions", "ticket_id")
    op.drop_table("approvals")
    op.drop_table("ticket_assignments")
    op.drop_table("tickets")
