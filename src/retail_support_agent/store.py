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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from retail_support_agent.auth import Identity
from retail_support_agent.db import (
    AuditEventRow,
    ConfirmationRow,
    IdentityRow,
    MessageRow,
    OperationRow,
    RoleMembershipRow,
    SessionClaimRow,
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
class SessionProgress:
    """Stop-condition counters plus the session's age, both read from the database."""

    steps: int
    tool_calls: int
    age_seconds: float


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

    # ---- identities and roles (M3) --------------------------------------
    def provision_identity(self, subject: str, user_id: str, roles: Iterable[str] = ()) -> None:
        """Map an authenticated subject to a retail identity and grant it roles.

        Provisioning is an administrative act, not something a client can do: there is
        no HTTP route to it. That is what stops "choose your own role at registration",
        which is the escalation this stage exists to prevent.

        Idempotent in both halves: the identity is updated rather than duplicated, and
        `(subject, role)` is a primary key, so re-granting a role conflicts and is
        ignored.
        """
        with self._sessions.begin() as db:
            row = db.get(IdentityRow, subject)
            if row is None:
                db.add(IdentityRow(subject=subject, user_id=user_id))
            else:
                row.user_id = user_id
            db.flush()
            for role in roles:
                stmt = (
                    pg_insert(RoleMembershipRow)
                    .values(subject=subject, role=role)
                    .on_conflict_do_nothing(index_elements=["subject", "role"])
                )
                db.execute(stmt)

    def revoke_role(self, subject: str, role: str) -> None:
        """Remove one role. It takes effect on the next request, which is the reason
        roles are read from here rather than from the token: a claim inside an already
        issued token cannot be withdrawn."""
        with self._sessions.begin() as db:
            row = db.get(RoleMembershipRow, (subject, role))
            if row is not None:
                db.delete(row)

    def identity_for(self, subject: str) -> Identity | None:
        """The retail identity and roles for a verified subject, or None if it has not
        been provisioned. A valid token for an unknown subject authenticates someone
        this system has no account for, which is a refusal, not a new account."""
        with self._sessions() as db:
            row = db.get(IdentityRow, subject)
            if row is None:
                return None
            roles = db.scalars(
                select(RoleMembershipRow.role).where(RoleMembershipRow.subject == subject)
            )
            return Identity(subject=subject, user_id=row.user_id, roles=frozenset(roles))

    # ---- sessions -------------------------------------------------------
    def ensure_session(
        self, session_id: str, user_id: str, subject: str | None = None
    ) -> str | None:
        """Create the session if absent, and return the subject that owns it.

        An existing `user_id` is never overwritten, which would silently change the
        identity of a session, and neither is an existing `subject`: the owner is
        whoever opened the session, so the returned value is what the caller compares
        against. Returning it from the same transaction that may have created the row
        means the check cannot read a row that a concurrent request has not written yet.
        """
        with self._sessions.begin() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                db.add(SessionRow(id=session_id, user_id=user_id, subject=subject))
                return subject
            return row.subject

    def session_subject(self, session_id: str) -> tuple[bool, str | None]:
        """(whether the session exists, which subject owns it).

        Two facts rather than one: a missing session and a session owned by nobody are
        different situations. Resetting a session that was never created is harmless,
        while continuing one that belongs to another subject is not.
        """
        with self._sessions() as db:
            row = db.get(SessionRow, session_id)
            return (row is not None, row.subject if row else None)

    def session_user(self, session_id: str) -> str | None:
        with self._sessions() as db:
            row = db.get(SessionRow, session_id)
            return row.user_id if row else None

    def progress(self, session_id: str) -> SessionProgress | None:
        """The stop-condition counters and how long the session has been open.

        `age_seconds` is computed by the database, not by the application: after a
        restart the process clock says nothing about when the session started, and two
        processes' clocks can differ.
        """
        with self._sessions() as db:
            stmt = select(
                SessionRow.steps,
                SessionRow.tool_calls,
                func.extract("epoch", func.now() - SessionRow.created_at),
            ).where(SessionRow.id == session_id)
            row = db.execute(stmt).one_or_none()
            if row is None:
                return None
            return SessionProgress(int(row[0]), int(row[1]), float(row[2]))

    def save_progress(self, session_id: str, steps: int, tool_calls: int) -> None:
        """Store the counters after a turn. Missing rows are ignored: without a session
        there is nothing to limit."""
        with self._sessions.begin() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                return
            row.steps = steps
            row.tool_calls = tool_calls

    # ---- claims ---------------------------------------------------------
    def claim_session(self, session_id: str, owner: str, ttl_seconds: float) -> bool:
        """Take or renew the lease on a session. False means another process holds it.

        One statement does the whole thing: insert the claim, and on conflict take it
        over only if this owner already holds it or the existing lease has expired. A
        read-then-write would let two processes both see a free session.

        The expiry is computed by the database (`now() + interval`), so two application
        processes with different clocks cannot disagree about when a lease ends.
        """
        expires = func.now() + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds)
        stmt = (
            pg_insert(SessionClaimRow)
            .values(session_id=session_id, owner=owner, expires_at=expires)
            .on_conflict_do_update(
                index_elements=[SessionClaimRow.session_id],
                set_={"owner": owner, "claimed_at": func.now(), "expires_at": expires},
                where=(SessionClaimRow.owner == owner) | (SessionClaimRow.expires_at < func.now()),
            )
        )
        with self._sessions.begin() as db:
            # RETURNING is the portable way to learn whether the conflict clause fired:
            # an ON CONFLICT DO UPDATE whose WHERE fails updates nothing and returns no
            # row, which is exactly "someone else holds the lease".
            taken = db.scalar(stmt.returning(SessionClaimRow.session_id))
            return taken is not None

    def release_session(self, session_id: str, owner: str) -> None:
        """Give up the lease. Another owner's claim is left alone: releasing a lease we
        do not hold would hand the session to a third process mid-turn."""
        with self._sessions.begin() as db:
            row = db.get(SessionClaimRow, session_id)
            if row is not None and row.owner == owner:
                db.delete(row)

    def claim_owner(self, session_id: str) -> str | None:
        """Who holds an unexpired lease, or None."""
        with self._sessions() as db:
            stmt = select(SessionClaimRow.owner).where(
                SessionClaimRow.session_id == session_id,
                SessionClaimRow.expires_at > func.now(),
            )
            return db.scalar(stmt)

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

    def confirmed_fingerprints(self, session_id: str) -> set[str]:
        """Every argument set the user has confirmed in this session.

        Recovery needs these: without them a write the user already authorized before a
        restart would be held for confirmation again, and the user would be asked twice
        for the same thing.
        """
        with self._sessions() as db:
            stmt = select(ConfirmationRow.fingerprint).where(
                ConfirmationRow.session_id == session_id,
                ConfirmationRow.state == "confirmed",
            )
            return set(db.scalars(stmt))

    def last_confirmation_id(self, session_id: str) -> int | None:
        """The most recent confirmed confirmation, used to rebuild the idempotency key
        after a restart. A different id would produce a different key, and the write
        would run a second time."""
        with self._sessions() as db:
            stmt = (
                select(ConfirmationRow.id)
                .where(
                    ConfirmationRow.session_id == session_id,
                    ConfirmationRow.state == "confirmed",
                )
                .order_by(ConfirmationRow.id.desc())
                .limit(1)
            )
            row = db.scalar(stmt)
            return int(row) if row is not None else None

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
