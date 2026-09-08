"""M1：应用侧状态（会话、消息、确认、写操作、审计）

Revision ID: 0001_application_state
Revises:
Create Date: 2026-09-08

**只建应用侧的表。** τ²-bench 的模拟订单数据不进数据库——那是基准的业务数据，
搬进来等于自己重写官方评测预期（见 docs/M1_PERSISTENCE.md §1）。

`downgrade` 是真的能跑的：迁移只能上不能下，等于第一次上线失败就没有退路。
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
        # 重发同一轮不会插出第二条——由唯一约束保证，不是应用层"先查有没有"
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
    # 部分唯一索引：一个会话同时最多一条 pending。只对 pending 生效，
    # 否则会变成"一个会话一辈子只能有一条确认"。
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
        # 幂等就靠这一条：同一个键第二次插入直接冲突，那就是"已经执行过"
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
