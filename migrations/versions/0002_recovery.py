"""M2: session claims and persisted stop-condition counters.

Revision ID: 0002_recovery
Revises: 0001_application_state
Create Date: 2026-09-08

The counters live on `sessions` rather than in a new table: they are one-per-session
facts, and a separate table would need its own row-creation path with nothing to gain.

`downgrade` really runs, for the same reason as 0001: a migration that only goes up
leaves no way back when a deployment fails.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_recovery"
down_revision = "0001_application_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows get 0 rather than NULL; the column is NOT NULL and
    # an ALTER without a default would fail on a non-empty table.
    op.add_column(
        "sessions",
        sa.Column("steps", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "sessions",
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "session_claims",
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # No default: a lease with no expiry is a lease that a crashed process holds
        # forever, so the caller must always say when it ends.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("session_claims")
    op.drop_column("sessions", "tool_calls")
    op.drop_column("sessions", "steps")
