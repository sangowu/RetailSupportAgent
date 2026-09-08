"""Reads and writes for application state. Transaction boundaries and idempotency
live here, not in the caller.

Three rules; everything else is detail:

1. One write action is one transaction. The operation row, the business effect and
   the audit event either all exist or none do. A partial record saying "logged but
   not executed" is worse than no record.
2. Idempotency comes from a unique index, not from check-then-insert. `SELECT, and
   INSERT if absent` misses under concurrency: two requests can both see nothing.
3. Confirmation fingerprints use the same definition as the guard. Changing an
   argument expires the confirmation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from retail_support_agent.db import (
    AuditEventRow,
    ConfirmationRow,
    MessageRow,
    OperationRow,
    SessionRow,
)
from retail_support_agent.guardrails import ProposedCall


def idempotency_key(session_id: str, call: ProposedCall, confirmation_id: int | None) -> str:
    """Key = session + tool name + canonical arguments + the confirmation that authorized it.

    Including confirmation_id is deliberate: a second confirmation of the same
    arguments is a new authorization and should run again, because the user may
    genuinely want to return another identical item. Without it, the second legitimate
    request would be treated as a retry and silently dropped.
    """
    canonical = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)
    raw = f"{session_id}|{call.name}|{canonical}|{confirmation_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class OperationOutcome:
    """Result of a write. `replayed=True` means nothing ran and this is the stored result."""

    operation_id: int
    result: str
    replayed: bool


class Store:
    """Repository for application state. Each method opens its own transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    # ---- sessions -------------------------------------------------------
    def ensure_session(self, session_id: str, user_id: str) -> None:
        """Create the session if absent. An existing `user_id` is never overwritten,
        which would silently change the identity of a session."""
        with self._sessions.begin() as db:
            if db.get(SessionRow, session_id) is None:
                db.add(SessionRow(id=session_id, user_id=user_id))

    def session_user(self, session_id: str) -> str | None:
        with self._sessions() as db:
            row = db.get(SessionRow, session_id)
            return row.user_id if row else None

    # ---- messages -------------------------------------------------------
    def append_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        content: str,
        tool_calls: list[str] | None = None,
    ) -> bool:
        """Append one turn. False means the turn already existed and nothing was inserted."""
        try:
            with self._sessions.begin() as db:
                db.add(
                    MessageRow(
                        session_id=session_id,
                        seq=seq,
                        role=role,
                        content=content,
                        tool_calls=list(tool_calls or []),
                    )
                )
        except IntegrityError:
            return False
        return True

    def messages(self, session_id: str) -> list[MessageRow]:
        with self._sessions() as db:
            stmt = (
                select(MessageRow)
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.seq)
            )
            return list(db.scalars(stmt))

    def next_seq(self, session_id: str) -> int:
        with self._sessions() as db:
            stmt = (
                select(MessageRow.seq)
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.seq.desc())
                .limit(1)
            )
            last = db.scalar(stmt)
            return (last or 0) + 1

    # ---- confirmations --------------------------------------------------
    def open_confirmation(self, session_id: str, call: ProposedCall) -> int:
        """Record a pending write action and return its id.

        An existing pending row is cancelled: changed arguments are a new proposal,
        and two pending rows would make "which one did the user confirm" a guess.
        """
        with self._sessions.begin() as db:
            stmt = select(ConfirmationRow).where(
                ConfirmationRow.session_id == session_id,
                ConfirmationRow.state == "pending",
            )
            for stale in db.scalars(stmt):
                stale.state = "cancelled"
                stale.resolved_at = _utcnow(db)
            db.flush()
            row = ConfirmationRow(
                session_id=session_id,
                tool_name=call.name,
                fingerprint=call.fingerprint,
                arguments=dict(call.arguments),
                state="pending",
            )
            db.add(row)
            db.flush()
            return int(row.id)

    def confirm(self, session_id: str, fingerprint: str) -> int | None:
        """Mark the pending confirmation as confirmed and return its id.

        A mismatched fingerprint confirms nothing and returns None: the user agreed to
        the arguments they were shown, so a changed set has to be asked again.
        """
        with self._sessions.begin() as db:
            stmt = select(ConfirmationRow).where(
                ConfirmationRow.session_id == session_id,
                ConfirmationRow.state == "pending",
            )
            row = db.scalars(stmt).one_or_none()
            if row is None or row.fingerprint != fingerprint:
                return None
            row.state = "confirmed"
            row.resolved_at = _utcnow(db)
            return int(row.id)

    def pending_confirmation(self, session_id: str) -> ConfirmationRow | None:
        with self._sessions() as db:
            stmt = select(ConfirmationRow).where(
                ConfirmationRow.session_id == session_id,
                ConfirmationRow.state == "pending",
            )
            return db.scalars(stmt).one_or_none()

    # ---- write actions (idempotent, transactional) ----------------------
    def execute_once(
        self,
        session_id: str,
        call: ProposedCall,
        confirmation_id: int | None,
        apply: Callable[[], str],
    ) -> OperationOutcome:
        """Run a write action; one idempotency key executes at most once.

        Order is claim the key, execute, then commit:

        1. Insert the operations row to claim the key. A conflict means it already
           ran, so the stored result is returned.
        2. Call `apply()` inside the same transaction to make the business change.
        3. Record the audit event and commit together.

        If `apply()` raises, the whole transaction rolls back: the operation row and
        the business effect are both absent, with no "logged but not done" or "done
        but not logged" middle state.

        `INSERT ... ON CONFLICT DO NOTHING` is deliberately not used: it does not say
        whether this caller inserted the row, so a second query would be needed
        anyway. Catching IntegrityError states the intent directly.
        """
        key = idempotency_key(session_id, call, confirmation_id)

        existing = self._find_operation(key)
        if existing is not None:
            return OperationOutcome(existing[0], existing[1], replayed=True)

        try:
            with self._sessions.begin() as db:
                op = OperationRow(
                    session_id=session_id,
                    confirmation_id=confirmation_id,
                    idempotency_key=key,
                    tool_name=call.name,
                    arguments=dict(call.arguments),
                    result="",
                    status="succeeded",
                )
                db.add(op)
                db.flush()  # hits the unique index here, before any business change

                result = apply()
                op.result = result
                db.add(
                    AuditEventRow(
                        session_id=session_id,
                        operation_id=op.id,
                        kind="write_executed",
                        detail={
                            "tool": call.name,
                            "fingerprint": call.fingerprint,
                            "confirmation_id": confirmation_id,
                        },
                    )
                )
                return OperationOutcome(int(op.id), result, replayed=False)
        except IntegrityError:
            # Another concurrent request claimed the key first. It executes; this one
            # returns its result.
            found = self._find_operation(key)
            if found is None:  # pragma: no cover - a conflict implies the row exists
                raise
            return OperationOutcome(found[0], found[1], replayed=True)

    def _find_operation(self, key: str) -> tuple[int, str] | None:
        with self._sessions() as db:
            stmt = select(OperationRow).where(OperationRow.idempotency_key == key)
            row = db.scalars(stmt).one_or_none()
            return (int(row.id), row.result) if row else None

    def operations(self, session_id: str) -> list[OperationRow]:
        with self._sessions() as db:
            stmt = (
                select(OperationRow)
                .where(OperationRow.session_id == session_id)
                .order_by(OperationRow.id)
            )
            return list(db.scalars(stmt))

    def audit_events(self, session_id: str) -> list[AuditEventRow]:
        with self._sessions() as db:
            stmt = (
                select(AuditEventRow)
                .where(AuditEventRow.session_id == session_id)
                .order_by(AuditEventRow.id)
            )
            return list(db.scalars(stmt))


def _utcnow(db: Session) -> Any:
    """The database's current time, not the application process's.

    Application and database clocks can differ by seconds. The audit timeline uses the
    database, otherwise the ordering of one sequence of events can contradict itself.
    """
    from sqlalchemy import func

    return db.scalar(select(func.now()))
