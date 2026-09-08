"""应用侧状态的表定义与连接。

**只放应用侧状态**：会话、消息、确认、写操作、审计。
τ²-bench 的模拟订单数据**不进这里**——那是基准的业务数据，搬进来等于自己
重写官方评测预期，成绩立刻不可比（见 docs/M1_PERSISTENCE.md §1）。

不变量交给数据库，不交给应用代码：唯一约束、外键、条件更新。
应用层"先查一下有没有"在并发下必然漏，唯一索引不会。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON, TypeEngine

# JSONB 只有 PostgreSQL 有。生产就是 PostgreSQL；留一个 JSON 回退是为了让
# 表定义在别的方言下也能建起来（例如离线看结构），不是为了支持第二种数据库。
JSON_TYPE: TypeEngine[Any] = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SessionRow(Base):
    """一次会话。user_id 是 τ²-bench 的模拟身份，不是真实用户。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MessageRow(Base):
    """逐轮对话。

    `(session_id, seq)` 唯一：客户端重发同一轮不会插出第二条，
    而不是靠应用层"先查有没有"——并发下那个检查必然漏。
    """

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_messages_session_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[Any] = mapped_column(JSON_TYPE, nullable=False, default=list)
    created_at: Mapped[dt.datetime] = _now()


class ConfirmationRow(Base):
    """一次待确认 / 已确认的写操作。

    `fingerprint` 与 M0.6 护栏用的是**同一套口径**（工具名 + 规范化参数的摘要）：
    参数变了指纹就变，旧确认自动失效。

    一个会话同时最多一条 pending——由部分唯一索引保证，不是应用层判断。
    """

    __tablename__ = "confirmations"
    __table_args__ = (
        CheckConstraint(
            "state in ('pending', 'confirmed', 'cancelled')", name="ck_confirmations_state"
        ),
        # 部分唯一索引：只对 pending 生效。写成普通唯一索引会变成
        # "一个会话一辈子只能有一条确认"，那显然不对。
        Index(
            "uq_confirmations_one_pending_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("state = 'pending'"),
            sqlite_where=text("state = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    arguments: Mapped[Any] = mapped_column(JSON_TYPE, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[dt.datetime] = _now()
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OperationRow(Base):
    """一次真正执行的写操作。

    幂等**靠 `idempotency_key` 的唯一索引**，不靠应用层"先查再插"：
    同一个键第二次到达时插入直接冲突，那就是"已经执行过"，取回原结果即可。
    """

    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operations_idempotency_key"),
        CheckConstraint("status in ('succeeded', 'failed')", name="ck_operations_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    confirmation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("confirmations.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[Any] = mapped_column(JSON_TYPE, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    created_at: Mapped[dt.datetime] = _now()


class AuditEventRow(Base):
    """授权依据与结果。回答"谁在什么时候授权了哪一次写操作"。"""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("operations.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[Any] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = _now()


def make_engine(url: str, echo: bool = False) -> Engine:
    """建连接池。

    `pool_pre_ping`：连接可能被数据库端或中间件悄悄断掉，不 ping 的话
    第一个用到它的请求会拿到一个已死的连接，报一个与业务无关的错。
    """
    return create_engine(url, echo=echo, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
