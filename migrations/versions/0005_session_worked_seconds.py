"""M8: the session's time budget is stored, not derived from the row's age.

Revision ID: 0005_session_worked_seconds
Revises: 0004_tickets
Create Date: 2026-09-21

The time limit was read as `now() - sessions.created_at`, which is how long a session
has existed rather than how long it has worked. An agent's case session is addressed by
an id derived from the ticket and the agent, deliberately, so that returning to a case
continues it - and that made every case permanently unworkable three minutes after it
was first opened.

`worked_seconds` accumulates time spent inside a turn. It is stored for the same reason
the step and tool-call counters are: a restart that reset it would make restarting a way
around the limit.

Existing rows start at 0, which is the lenient direction. The alternative - seeding from
`created_at` - would carry the defect forward into exactly the rows it broke.

`downgrade` really runs, for the same reason as 0001 through 0004.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_session_worked_seconds"
down_revision = "0004_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "worked_seconds",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "worked_seconds")
