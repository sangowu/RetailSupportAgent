"""M3: identities, role memberships and session ownership.

Revision ID: 0003_identities
Revises: 0002_recovery
Create Date: 2026-09-08

`sessions.subject` is nullable on purpose. Rows created before this migration have no
subject, and the demo and evaluation paths run without authentication, so a NOT NULL
column would either need an invented placeholder or would stop those paths from
creating a session at all. The ownership check reads NULL as "unowned", never as a match.

`downgrade` really runs, for the same reason as 0001 and 0002.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_identities"
down_revision = "0002_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identities",
        # The token's `sub`: already unique and stable, so it is the key. A surrogate id
        # would only add a way for two rows to claim one subject.
        sa.Column("subject", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_identities_user_id", "identities", ["user_id"])

    op.create_table(
        "role_memberships",
        sa.Column(
            "subject",
            sa.String(128),
            sa.ForeignKey("identities.subject", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Part of the primary key, so granting the same role twice is idempotent rather
        # than a duplicate row.
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.add_column(
        "sessions",
        sa.Column(
            "subject",
            sa.String(128),
            # SET NULL rather than CASCADE: deleting an identity must not delete the
            # audit trail of what it did. The session survives with no owner, which the
            # ownership check refuses under authentication, so it becomes unreachable
            # rather than open.
            sa.ForeignKey("identities.subject", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_sessions_subject", "sessions", ["subject"])


def downgrade() -> None:
    op.drop_index("ix_sessions_subject", table_name="sessions")
    op.drop_column("sessions", "subject")
    op.drop_table("role_memberships")
    op.drop_index("ix_identities_user_id", table_name="identities")
    op.drop_table("identities")
