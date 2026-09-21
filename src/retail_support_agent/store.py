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

import datetime as dt
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
    ApprovalRow,
    AuditEventRow,
    ConfirmationRow,
    IdentityRow,
    MessageRow,
    OperationRow,
    RoleMembershipRow,
    SessionClaimRow,
    SessionRow,
    TicketAssignmentRow,
    TicketRow,
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
    """Everything the stop conditions need to resume, read from the database."""

    steps: int
    tool_calls: int
    worked_seconds: float


# How long a human decision stays valid. The architecture document fixes the default at
# 30 minutes: long enough for someone to read a case, short enough that an authorization
# is not sitting in a table waiting to be used tomorrow. Callers may pass their own.
APPROVAL_TTL_SECONDS = 30 * 60.0


@dataclass(frozen=True)
class TicketSummary:
    """A ticket as the API reports it, with its current assignee resolved."""

    id: int
    session_id: str
    user_id: str
    state: str
    summary: str
    requested_by: str | None
    assignee: str | None
    assignee_user_id: str | None
    resolution: str
    created_at: dt.datetime
    closed_at: dt.datetime | None

    @property
    def is_open(self) -> bool:
        return self.state in ("open", "assigned")


@dataclass(frozen=True)
class SessionTiming:
    """How long a session took, split by who was holding it up.

    Reporting one total would make a session look slower the longer a person took to
    answer, and every latency comparison against a baseline would stop meaning anything.
    """

    total_seconds: float
    waiting_seconds: float

    @property
    def system_seconds(self) -> float:
        return max(0.0, self.total_seconds - self.waiting_seconds)


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

    def identities_with_role(self, role: str) -> list[Identity]:
        """Everyone the database grants one role, for the page that has to name them.

        Assignment takes a subject, and a subject is an opaque identifier nobody can type
        from memory. Without this a supervisor could only assign a case by pasting a
        string they have no way to obtain, so the control would exist and be unusable.

        A read of what the database already decided, offered only to the role that has to
        act on it: the route is gated on `ticket:assign`.
        """
        with self._sessions() as db:
            subjects = list(
                db.scalars(
                    select(RoleMembershipRow.subject)
                    .where(RoleMembershipRow.role == role)
                    .order_by(RoleMembershipRow.subject)
                )
            )
            found = []
            for subject in subjects:
                identity = db.get(IdentityRow, subject)
                if identity is None:
                    continue
                roles = db.scalars(
                    select(RoleMembershipRow.role).where(RoleMembershipRow.subject == subject)
                )
                found.append(
                    Identity(subject=subject, user_id=identity.user_id, roles=frozenset(roles))
                )
            return found

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
        """The stop-condition counters and the time already spent working.

        The worked time is stored rather than derived from `created_at`. Deriving it was
        the defect: an agent's case session is addressed by an id derived from the ticket
        and the agent, so returning to a case reaches the same row, and reading its age
        as the elapsed time made every case unworkable three minutes after it was first
        opened.
        """
        with self._sessions() as db:
            stmt = select(
                SessionRow.steps,
                SessionRow.tool_calls,
                SessionRow.worked_seconds,
            ).where(SessionRow.id == session_id)
            row = db.execute(stmt).one_or_none()
            if row is None:
                return None
            return SessionProgress(int(row[0]), int(row[1]), float(row[2]))

    def save_progress(
        self, session_id: str, steps: int, tool_calls: int, worked_seconds: float = 0.0
    ) -> None:
        """Store the counters and the worked time after a turn. Missing rows are ignored:
        without a session there is nothing to limit."""
        with self._sessions.begin() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                return
            row.steps = steps
            row.tool_calls = tool_calls
            row.worked_seconds = worked_seconds

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

    def pending_confirmation_count(self) -> int:
        """How many writes are waiting for someone to confirm them, in any session.

        Used to decide whether the service is idle. A confirmation nobody has answered
        looks exactly like an empty machine from outside, and is not one: the write was
        proposed and the answer is still owed.
        """
        with self._sessions() as db:
            stmt = (
                select(func.count())
                .select_from(ConfirmationRow)
                .where(ConfirmationRow.state == "pending")
            )
            return int(db.scalar(stmt) or 0)

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

    def record_access_events(self, session_id: str, rows: list[dict[str, str]]) -> int:
        """Persist this turn's access decisions. Returns how many were written.

        Written after the turn rather than inside it, and in their own transaction: an
        access decision is not part of the business change the way `write_executed` is,
        and a failure to record one must not roll back a write the customer already
        confirmed. The cost of that choice is stated plainly - a crash between the turn
        and this call loses the turn's access records, and the alternative would have
        been to let audit bookkeeping undo real work.

        `operation_id` stays null: a refusal has no operation, which is the point of it.
        """
        if not rows:
            return 0
        with self._sessions.begin() as db:
            for row in rows:
                db.add(
                    AuditEventRow(
                        session_id=session_id,
                        operation_id=None,
                        kind="access_denied" if row["verdict"] == "deny" else "output_withheld",
                        detail={
                            "tool": row["tool"],
                            "reason": row["reason"],
                            "authority": row["source"],
                        },
                    )
                )
        return len(rows)

    def audit_events(self, session_id: str) -> list[AuditEventRow]:
        with self._sessions() as db:
            stmt = (
                select(AuditEventRow)
                .where(AuditEventRow.session_id == session_id)
                .order_by(AuditEventRow.id)
            )
            return list(db.scalars(stmt))

    # ---- tickets, assignments and approvals (M4) ------------------------
    def open_ticket(
        self, session_id: str, user_id: str, summary: str, requested_by: str | None
    ) -> TicketSummary:
        """Record a handoff to a human, or return the ticket this session already has.

        A second handoff on a session that is already waiting is the same case, not a new
        one. The partial unique index says so; this method reads it back rather than
        letting the insert fail, because "you already asked" is a normal outcome.
        """
        existing = self.open_ticket_for(session_id)
        if existing is not None:
            return existing
        with self._sessions.begin() as db:
            row = TicketRow(
                session_id=session_id,
                user_id=user_id,
                summary=summary,
                requested_by=requested_by,
                state="open",
            )
            db.add(row)
            db.flush()
            ticket_id = int(row.id)
        found = self.ticket(ticket_id)
        assert found is not None  # just inserted
        return found

    def ticket(self, ticket_id: int) -> TicketSummary | None:
        with self._sessions() as db:
            row = db.get(TicketRow, ticket_id)
            return self._summarize(db, row) if row is not None else None

    def open_ticket_for(self, session_id: str) -> TicketSummary | None:
        """The live ticket suspending this session, if there is one."""
        with self._sessions() as db:
            stmt = select(TicketRow).where(
                TicketRow.session_id == session_id,
                TicketRow.state.in_(("open", "assigned")),
            )
            row = db.scalars(stmt).one_or_none()
            return self._summarize(db, row) if row is not None else None

    def tickets(self, assignee: str | None = None, open_only: bool = False) -> list[TicketSummary]:
        """Tickets, oldest first. `assignee` restricts to cases currently held by one
        person, which is how an agent sees their own queue and nobody else's."""
        with self._sessions() as db:
            stmt = select(TicketRow).order_by(TicketRow.id)
            if open_only:
                stmt = stmt.where(TicketRow.state.in_(("open", "assigned")))
            if assignee is not None:
                stmt = stmt.join(
                    TicketAssignmentRow, TicketAssignmentRow.ticket_id == TicketRow.id
                ).where(
                    TicketAssignmentRow.assignee == assignee,
                    TicketAssignmentRow.released_at.is_(None),
                )
            return [self._summarize(db, row) for row in db.scalars(stmt)]

    def assign_ticket(self, ticket_id: int, assignee: str, assigned_by: str) -> bool:
        """Give a ticket to someone, releasing whoever held it. False if it is closed.

        Reassignment releases rather than overwrites: who could reach a customer's orders,
        and when, has to stay readable afterwards.
        """
        with self._sessions.begin() as db:
            ticket = db.get(TicketRow, ticket_id)
            if ticket is None or ticket.state not in ("open", "assigned"):
                return False
            stmt = select(TicketAssignmentRow).where(
                TicketAssignmentRow.ticket_id == ticket_id,
                TicketAssignmentRow.released_at.is_(None),
            )
            for held in db.scalars(stmt):
                held.released_at = _utcnow(db)
            db.flush()
            db.add(
                TicketAssignmentRow(ticket_id=ticket_id, assignee=assignee, assigned_by=assigned_by)
            )
            ticket.state = "assigned"
            return True

    def close_ticket(self, ticket_id: int, state: str, resolution: str) -> bool:
        """Resolve or reject a ticket and release its assignment. False if already closed."""
        if state not in ("resolved", "rejected"):
            raise ValueError("a ticket closes as resolved or rejected")
        with self._sessions.begin() as db:
            ticket = db.get(TicketRow, ticket_id)
            if ticket is None or ticket.state not in ("open", "assigned"):
                return False
            stmt = select(TicketAssignmentRow).where(
                TicketAssignmentRow.ticket_id == ticket_id,
                TicketAssignmentRow.released_at.is_(None),
            )
            for held in db.scalars(stmt):
                held.released_at = _utcnow(db)
            ticket.state = state
            ticket.resolution = resolution
            ticket.closed_at = _utcnow(db)
            return True

    def assignee_of(self, ticket_id: int) -> str | None:
        """Who currently holds the ticket. Read per request rather than cached: a released
        assignment must stop granting access immediately, the same rule M3 applies to
        roles."""
        with self._sessions() as db:
            return self._assignee(db, ticket_id)[0]

    def open_approval(
        self,
        session_id: str,
        call: ProposedCall,
        requested_by: str,
        ticket_id: int | None = None,
        ttl_seconds: float = APPROVAL_TTL_SECONDS,
    ) -> int:
        """Ask a human to decide one specific action, and return the request's id.

        A pending request for the same fingerprint is reused, so a model proposing the
        same call twice does not fill a supervisor's queue with copies of one decision.
        """
        with self._sessions.begin() as db:
            stmt = select(ApprovalRow).where(
                ApprovalRow.session_id == session_id,
                ApprovalRow.fingerprint == call.fingerprint,
                ApprovalRow.state == "pending",
                ApprovalRow.expires_at > func.now(),
            )
            existing = db.scalars(stmt).first()
            if existing is not None:
                return int(existing.id)
            row = ApprovalRow(
                ticket_id=ticket_id,
                session_id=session_id,
                tool_name=call.name,
                fingerprint=call.fingerprint,
                arguments=dict(call.arguments),
                requested_by=requested_by,
                state="pending",
                # The database sets the deadline, not the application: two processes with
                # different clocks must not disagree about when an authorization ends.
                expires_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds),
            )
            db.add(row)
            db.flush()
            return int(row.id)

    def decide_approval(self, approval_id: int, approve: bool, decided_by: str) -> str:
        """Approve or reject a pending request. Returns what happened.

        The rule that nobody decides their own escalation is enforced here rather than in
        the API, because it is a property of the record: a second entry point must not be
        able to skip it.
        """
        with self._sessions.begin() as db:
            row = db.get(ApprovalRow, approval_id)
            if row is None:
                return "not_found"
            if row.state != "pending":
                return "already_decided"
            if row.requested_by == decided_by:
                return "self_decision"
            if db.scalar(select(func.now() > row.expires_at)):
                return "expired"
            row.state = "approved" if approve else "rejected"
            row.decided_by = decided_by
            row.decided_at = _utcnow(db)
            return str(row.state)

    def approval(self, approval_id: int) -> ApprovalRow | None:
        with self._sessions() as db:
            return db.get(ApprovalRow, approval_id)

    def pending_approvals(self, session_id: str | None = None) -> list[ApprovalRow]:
        """Requests still waiting for a human, excluding those that ran out of time.

        An expired request is not offered as pending: deciding it would produce an
        approval that authorizes nothing, which is worse than not offering the button.
        """
        with self._sessions() as db:
            stmt = select(ApprovalRow).where(
                ApprovalRow.state == "pending", ApprovalRow.expires_at > func.now()
            )
            if session_id is not None:
                stmt = stmt.where(ApprovalRow.session_id == session_id)
            return list(db.scalars(stmt.order_by(ApprovalRow.id)))

    def approved_fingerprints(self, session_id: str) -> set[str]:
        """Argument sets a human approved and that are still valid.

        Expiry is applied by the database in the same query rather than compared in
        Python afterwards, so one approval cannot be valid for one reader and expired for
        another.
        """
        with self._sessions() as db:
            stmt = select(ApprovalRow.fingerprint).where(
                ApprovalRow.session_id == session_id,
                ApprovalRow.state == "approved",
                ApprovalRow.expires_at > func.now(),
            )
            return set(db.scalars(stmt))

    def bind_session_to_ticket(
        self, session_id: str, user_id: str, subject: str, ticket_id: int
    ) -> None:
        """Mark a session as an agent working one ticket, creating it if needed."""
        with self._sessions.begin() as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                db.add(
                    SessionRow(id=session_id, user_id=user_id, subject=subject, ticket_id=ticket_id)
                )
                return
            row.ticket_id = ticket_id

    def session_ticket(self, session_id: str) -> int | None:
        with self._sessions() as db:
            row = db.get(SessionRow, session_id)
            return row.ticket_id if row else None

    def session_timing(self, session_id: str) -> SessionTiming:
        """Total elapsed time, and the part of it a human was holding.

        Waiting intervals are merged before they are summed: a ticket open while an
        approval is pending is one wait, and adding both would report more waiting than
        the session has existed.
        """
        spans: list[tuple[dt.datetime, dt.datetime]] = []
        with self._sessions() as db:
            session = db.get(SessionRow, session_id)
            if session is None:
                return SessionTiming(0.0, 0.0)
            now: dt.datetime | None = db.scalar(select(func.now()))
            assert now is not None  # SELECT now() always returns a row
            total = (now - session.created_at).total_seconds()

            for ticket in db.scalars(select(TicketRow).where(TicketRow.session_id == session_id)):
                spans.append((ticket.created_at, ticket.closed_at or now))
            for approval in db.scalars(
                select(ApprovalRow).where(ApprovalRow.session_id == session_id)
            ):
                end = approval.decided_at or min(approval.expires_at, now)
                if end > approval.created_at:
                    spans.append((approval.created_at, end))

        waiting = _merged_seconds(spans)
        return SessionTiming(total_seconds=total, waiting_seconds=min(waiting, total))

    # ---- shared helpers -------------------------------------------------
    def _assignee(self, db: Session, ticket_id: int) -> tuple[str | None, str | None]:
        """The subject holding the ticket, and the retail identity that subject maps to.

        Both are read here because only this layer can read both. The subject is what
        every access check compares against; the retail identity is the only half a
        person can recognise, and resolving it anywhere else meant reading the account
        roster, which is gated on `ticket:assign`. An agent does not hold that, so the
        assignee arrived at their page as a raw subject - the one role that reads it all
        day. The join is outer: an assignment to a subject with no identity row is a
        provisioning gap, not a reason to drop the case from the list.
        """
        stmt = (
            select(TicketAssignmentRow.assignee, IdentityRow.user_id)
            .outerjoin(IdentityRow, IdentityRow.subject == TicketAssignmentRow.assignee)
            .where(
                TicketAssignmentRow.ticket_id == ticket_id,
                TicketAssignmentRow.released_at.is_(None),
            )
        )
        held = db.execute(stmt).one_or_none()
        return (None, None) if held is None else (held[0], held[1])

    def _summarize(self, db: Session, row: TicketRow) -> TicketSummary:
        assignee, assignee_user_id = self._assignee(db, int(row.id))
        return TicketSummary(
            id=int(row.id),
            session_id=row.session_id,
            user_id=row.user_id,
            state=row.state,
            summary=row.summary,
            requested_by=row.requested_by,
            assignee=assignee,
            assignee_user_id=assignee_user_id,
            resolution=row.resolution,
            created_at=row.created_at,
            closed_at=row.closed_at,
        )


def _merged_seconds(spans: list[tuple[dt.datetime, dt.datetime]]) -> float:
    """Total length of a set of intervals, counting overlapping time once."""
    if not spans:
        return 0.0
    ordered = sorted(spans, key=lambda span: span[0])
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start > current_end:
            total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        elif end > current_end:
            current_end = end
    total += (current_end - current_start).total_seconds()
    return total


def _utcnow(db: Session) -> Any:
    """The database's current time, not the application process's.

    Application and database clocks can differ by seconds. The audit timeline uses the
    database, otherwise the ordering of one sequence of events can contradict itself.
    """
    from sqlalchemy import func

    return db.scalar(select(func.now()))
