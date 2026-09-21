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
    Float,
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
    """One session. `user_id` is a τ²-bench simulated identity, not a real user.

    `subject` records who opened it, so a second caller cannot continue someone else's
    conversation - a different question from M2's claim, which records which *process*
    is driving it.

    `steps` and `tool_calls` are the session's stop-condition counters (M2). They are
    persisted rather than derived, because a restart that reset them to zero would make
    restarting a way around the limits, and no other table records them: `messages` has
    one row per visible turn, not per model step.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The authenticated subject that opened the session (M3), or NULL when
    # authentication is not configured. Nullable because the demo and evaluation paths
    # legitimately have no subject, and rows created before M3 have none either; the
    # ownership check treats NULL as "unowned" rather than as a match.
    subject: Mapped[str | None] = mapped_column(
        String(128),
        # SET NULL, not CASCADE: deleting an identity must not delete the record of what
        # it did. The session is left unowned, which the ownership check refuses.
        ForeignKey("identities.subject", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The ticket this session is working, for a session an agent opened on a customer's
    # behalf (M4). NULL for a customer's own session. It is what makes "acting for
    # someone else" a stored fact rather than a claim in a request.
    #
    # No foreign key, deliberately: `tickets.session_id` already points the other way,
    # and a second constraint closing the cycle would make either row impossible to
    # insert first. The value is written only after the ticket exists.
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Seconds this session has spent inside a turn. Persisted for the same reason as the
    # counters: a restart that reset it would make restarting a way around the time
    # limit. It is not derivable from `created_at` - that is how long the session has
    # existed, which is what this replaces.
    worked_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IdentityRow(Base):
    """An authenticated subject mapped to the retail identity that owns its orders (M3).

    `subject` is the primary key rather than a surrogate id: it is the token's `sub`,
    already unique and stable for the life of the account, and a second key would only
    create a way for two rows to claim one subject.

    The mapping exists so the retail `user_id` is derived on the server. A request body
    can never say whose orders it is operating on.
    """

    __tablename__ = "identities"

    subject: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = _now()


class RoleMembershipRow(Base):
    """A role granted to a subject (M3).

    Roles live here and not in the token. A group claim is trusted only as far as every
    path that can write it, and a token minted before a role was revoked keeps asserting
    the role until it expires; a row that is deleted stops granting anything on the next
    request.

    `(subject, role)` is the primary key, so granting the same role twice is not an error
    and cannot produce a duplicate.
    """

    __tablename__ = "role_memberships"

    subject: Mapped[str] = mapped_column(
        String(128), ForeignKey("identities.subject", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    granted_at: Mapped[dt.datetime] = _now()


class SessionClaimRow(Base):
    """A lease saying which process currently drives a session (M2).

    One session must be driven by one process at a time. Two processes replaying and
    advancing the same session would interleave their writes, and the confirmation
    fingerprint of one could authorize the other's call.

    The lease expires rather than being held forever: a process that crashes mid-turn
    never releases anything, and without an expiry that session would be unusable until
    someone deleted the row by hand.
    """

    __tablename__ = "session_claims"

    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    claimed_at: Mapped[dt.datetime] = _now()
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TicketRow(Base):
    """One human handoff (M4).

    Created when the agent transfers a case to a human. The session is suspended while
    the ticket is open: without that, the model would keep working on a case it had just
    escalated, which is the failure this table exists to prevent.

    `user_id` is copied rather than read through the session, because the scope check
    ("may this agent act for that customer") must not depend on the session row still
    being reachable, and because a ticket outlives the session that raised it.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "state in ('open', 'assigned', 'resolved', 'rejected')", name="ck_tickets_state"
        ),
        # One open ticket per session. A second handoff on a session that is already
        # waiting for a human is the same case, not a new one, and two open tickets
        # would make "is this session suspended" ambiguous.
        Index(
            "uq_tickets_one_open_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("state in ('open', 'assigned')"),
            sqlite_where=text("state in ('open', 'assigned')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Who asked for a human. NULL when authentication is off, where there is no subject.
    requested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    resolution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[dt.datetime] = _now()
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TicketAssignmentRow(Base):
    """Who is working a ticket, and who was working it before (M4).

    History rather than a column on `tickets`: reassignment is a supervisor action that
    must stay visible afterwards, and an assignment that is simply overwritten leaves no
    record of who had access to a customer's orders and when.

    The partial unique index allows one active assignment per ticket, the same mechanism
    that gives a session one pending confirmation.
    """

    __tablename__ = "ticket_assignments"
    __table_args__ = (
        Index(
            "uq_ticket_assignments_one_active",
            "ticket_id",
            unique=True,
            postgresql_where=text("released_at is null"),
            sqlite_where=text("released_at is null"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assignee: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    assigned_by: Mapped[str] = mapped_column(String(128), nullable=False)
    assigned_at: Mapped[dt.datetime] = _now()
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalRow(Base):
    """A human decision on one specific action (M4).

    Bound to a fingerprint, not to an action type, so approving "cancel #W001" cannot
    authorize "cancel #W002" and changing an argument invalidates it. That is the same
    rule confirmations follow, for the same reason.

    `expires_at` is set by the database clock at creation. An approval that never expires
    is an authorization sitting in a table waiting to be used months later, and the
    architecture document requires it to be re-requested instead.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("state in ('pending', 'approved', 'rejected')", name="ck_approvals_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    arguments: Mapped[Any] = mapped_column(JSON_TYPE, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[dt.datetime] = _now()
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
