"""Table definitions and connection for application state.

Application state only: sessions, messages, confirmations, write operations, audit
events. The τ²-bench simulated order data stays out of this database. It is the
benchmark's business data, and moving it here would amount to rewriting the official
evaluation expectations, making scores incomparable (docs/M1_PERSISTENCE.md §1).

Invariants belong to the database, not to application code: unique constraints,
foreign keys, conditional updates. An application-level "check whether it exists"
misses under concurrency; a unique index does not.
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

# JSONB is PostgreSQL-only, and PostgreSQL is what production uses. The JSON fallback
# exists so the tables can still be created under another dialect for inspection, not
# to support a second database.
JSON_TYPE: TypeEngine[Any] = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SessionRow(Base):
    """One session. `user_id` is a τ²-bench simulated identity, not a real user."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MessageRow(Base):
    """One conversation turn.

    `(session_id, seq)` is unique, so a resent turn cannot insert a second row. The
    constraint does this, not an application-level existence check, which misses under
    concurrency.
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
    """A pending or resolved confirmation for one write action.

    `fingerprint` uses the same definition as the guard (tool name plus canonical
    arguments), so changing an argument expires the confirmation.

    A session has at most one pending confirmation, enforced by a partial unique
    index rather than by application logic.
    """

    __tablename__ = "confirmations"
    __table_args__ = (
        CheckConstraint(
            "state in ('pending', 'confirmed', 'cancelled')", name="ck_confirmations_state"
        ),
        # Partial unique index, pending only. A plain unique index would mean one
        # confirmation per session for the lifetime of the session.
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
    """One executed write action.

    Idempotency comes from the unique index on `idempotency_key`, not from a
    check-then-insert: a second insert of the same key conflicts, and that conflict is
    the signal that the operation already ran.
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
    """Authorization basis and outcome: who authorized which write action, and when."""

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
    """Build the connection pool.

    `pool_pre_ping` is on because a connection can be dropped by the server or an
    intermediary. Without it, the next request to use that connection fails with an
    error unrelated to what it was doing.
    """
    return create_engine(url, echo=echo, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
