"""Hard limits on write actions: ownership checks, argument-bound confirmation, stop conditions.

These are the M0.3 baseline requirements from DEVELOPMENT_PLAN.md, implemented as
pure logic. The model cannot change these verdicts. Every write action passes through here.

Confirmation is bound to specific arguments. The user confirms "cancel order #W123",
not "are you sure". If the model changes an argument afterwards the fingerprint no
longer matches and the call needs a new confirmation.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from retail_support_agent.auth import DEFAULT_PERMISSIONS, Permission
from retail_support_agent.domain import WRITE_TOOLS
from retail_support_agent.scope import CATALOG_TOOLS, DISCOVERY_TOOLS, AccessScope, SliceAudit

# Argument names that may carry an order or user id. Used by the ownership check.
ORDER_ID_FIELDS = ("order_id",)
USER_ID_FIELDS = ("user_id",)


class Decision(StrEnum):
    """Verdict on one proposed tool call."""

    ALLOW = "allow"  # read action, or a write whose fingerprint is already confirmed
    NEEDS_CONFIRMATION = "needs_confirmation"  # write action, not confirmed for these arguments
    DENIED_OWNERSHIP = "denied_ownership"  # order does not belong to the session identity
    DENIED_PERMISSION = "denied_permission"  # the account's roles do not grant this action
    NEEDS_APPROVAL = "needs_approval"  # acting for another user: a human has not decided yet
    DENIED_UNKNOWN_TOOL = "denied_unknown_tool"  # not in the upstream tool list
    DENIED_SLICE = "denied_slice"  # outside the session's access scope (reads, status, lookups)


# Refusals that mean "you may not reach this", and so count towards the probe limit.
# NEEDS_CONFIRMATION and NEEDS_APPROVAL are excluded: they mean "not yet", and a session
# that patiently confirms each action is the behaviour this system wants. So is
# DENIED_UNKNOWN_TOOL, which is a model that invented a tool name rather than an account
# reaching for data - ending the session over the model's own mistake would be a worse
# answer than telling it the tool does not exist.
COUNTED_DENIALS: frozenset[Decision] = frozenset(
    {Decision.DENIED_OWNERSHIP, Decision.DENIED_PERMISSION, Decision.DENIED_SLICE}
)


@dataclass(frozen=True)
class ProposedCall:
    """One tool call proposed by the model."""

    name: str
    arguments: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        """Digest of tool name plus canonical arguments.

        Changing an argument changes the fingerprint, which expires any earlier
        confirmation.
        """
        canonical = json.dumps(self.arguments, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(f"{self.name}|{canonical}".encode()).hexdigest()

    def describe(self) -> str:
        """Confirmation text shown to the user, listing every real argument.

        English, because the τ²-bench conversation is English throughout. A Chinese
        prompt made the simulated user spend a turn guessing what language it was;
        that cost task 80 its score.
        """
        if not self.arguments:
            return f"Run {self.name} (no arguments)."
        items = "\n".join(
            f"  - {key}: {json.dumps(value, ensure_ascii=False, default=str)}"
            for key, value in sorted(self.arguments.items())
        )
        return f"Run {self.name} with these arguments:\n{items}"


@dataclass
class GuardVerdict:
    """A verdict. On denial, `reason` is shown to the user verbatim."""

    decision: Decision
    reason: str = ""
    call: ProposedCall | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass
class SessionLimits:
    """Stop conditions, so one session cannot call tools indefinitely."""

    max_steps: int = 20
    max_tool_calls: int = 15
    timeout_seconds: float = 180.0
    # A session that keeps proposing calls it is not allowed to make is probing, whether
    # a person is steering it or the model is looping. Five is above what a confused
    # model produces in a legitimate conversation and far below what mapping somebody
    # else's orders would take. Reaching it stops the session rather than the call: the
    # refusals themselves are already correct, and what this bounds is how many of them
    # one session may collect.
    max_denials: int = 5


@dataclass
class SessionState:
    """Per-session state. One session is processed serially."""

    user_id: str
    limits: SessionLimits = field(default_factory=SessionLimits)
    # What the account's roles allow (M3). The default is the customer set, which is
    # what the demo and evaluation paths run as; under authentication it is whatever
    # `role_memberships` grants, never what the token claims.
    permissions: frozenset[Permission] = DEFAULT_PERMISSIONS
    # True when a support agent is working this session on a customer's behalf under an
    # assigned ticket (M4). Such a write needs a human decision as well as the customer's
    # confirmation; a customer acting on their own orders does not.
    acting_for_other: bool = False
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    tool_calls: int = 0
    # Fingerprints the user has confirmed. One confirmation covers one argument set.
    confirmed_fingerprints: set[str] = field(default_factory=set)
    # Argument sets a supervisor has approved and that have not expired (M4). Refreshed
    # from the database each turn rather than accumulated in memory, so a decision that
    # has run out of time stops authorizing anything.
    approved_fingerprints: set[str] = field(default_factory=set)
    # The slice of retail data this session may reach. The permissive default keeps the
    # benchmark adapter and every pre-existing caller behaving exactly as before; the
    # API replaces it per turn, from the same identity and assignment it reads roles from.
    scope: AccessScope = field(default_factory=AccessScope.permissive)
    slice_audit: SliceAudit = field(default_factory=SliceAudit)
    # Refused proposals so far. Counted by `WriteGuard.review`, not by the callers, so
    # that no refusal path can forget to count itself.
    denials: int = 0
    # Identifiers this turn is entitled to say back: what its tool results contained,
    # plus what the user typed. Cleared at the start of every turn, so an answer cannot
    # be assembled from what an earlier turn read under permissions since revoked.
    disclosed_ids: set[str] = field(default_factory=set)
    pending: ProposedCall | None = None

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def stop_reason(self) -> str | None:
        """Why the session must stop, or None if it may continue."""
        if self.steps >= self.limits.max_steps:
            return f"Reached the step limit of {self.limits.max_steps}."
        if self.tool_calls >= self.limits.max_tool_calls:
            return f"Reached the tool-call limit of {self.limits.max_tool_calls}."
        if self.elapsed() >= self.limits.timeout_seconds:
            return f"Exceeded the session time limit of {self.limits.timeout_seconds:.0f}s."
        if self.denials >= self.limits.max_denials:
            return f"Reached the refused-action limit of {self.limits.max_denials}."
        return None

    def confirm(self, call: ProposedCall) -> None:
        """Record the user's confirmation of one specific argument set."""
        self.confirmed_fingerprints.add(call.fingerprint)
        self.pending = None


class WriteGuard:
    """Rules on every tool call. Does not rely on the model policing itself.

    Order of checks: known tool, then permission, then ownership, then approval (only
    when acting for another user), then confirmation. Each one is stricter than the last,
    and the reason given is the first that fails, so a refusal never reveals more than
    the caller was entitled to ask.
    """

    def __init__(
        self,
        known_tools: set[str],
        owner_lookup: Any,
        status_lookup: Any = None,
    ) -> None:
        """`owner_lookup(order_id) -> user_id | None`, supplied by RetailDomain.

        `status_lookup(order_id) -> status | None` is optional and only consulted when
        the session scope narrows writes to a lifecycle stage. It stays optional because
        the benchmark adapter answers ownership from what the conversation already looked
        up and must not be made to spend a tool call on a status it never asked for.
        """
        self._known_tools = known_tools
        self._owner_lookup = owner_lookup
        self._status_lookup = status_lookup

    def review(self, call: ProposedCall, state: SessionState) -> GuardVerdict:
        """Judge one call and count it if it was refused.

        The counting is here rather than at the call sites so that a refusal path added
        later cannot forget to count itself, which is the way a probe limit stops
        working without anyone noticing.
        """
        verdict = self._review(call, state)
        if verdict.decision in COUNTED_DENIALS:
            state.denials += 1
            # Recorded here for the same reason it is counted here. An ownership refusal
            # is raised deep in the write path and a slice refusal in the read path; a
            # recording call at each site is one that a later refusal path will not make,
            # and the access-control evaluation found exactly that gap - refused writes
            # were reaching no audit trail at all.
            state.slice_audit.record(call.name, "deny", verdict.reason, state.scope.source)
        return verdict

    def _review(self, call: ProposedCall, state: SessionState) -> GuardVerdict:
        if call.name not in self._known_tools:
            return GuardVerdict(
                Decision.DENIED_UNKNOWN_TOOL,
                f"{call.name} is not in this domain's tool list. Refusing to run it.",
                call,
            )
        if call.name not in WRITE_TOOLS:
            if Permission.ORDER_READ not in state.permissions:
                return GuardVerdict(
                    Decision.DENIED_PERMISSION,
                    f"This account is not allowed to read order data, so {call.name} will not run.",
                    call,
                )
            return self._check_read_scope(call, state)

        # Permission before ownership, and both before the confirmation. An account
        # with no write permission must be refused whether or not it owns the order,
        # and answering "that order is not yours" first would tell it which orders
        # exist.
        if Permission.ORDER_WRITE not in state.permissions:
            return GuardVerdict(
                Decision.DENIED_PERMISSION,
                f"This account is not allowed to change orders, so {call.name} will "
                "not run. A confirmation does not grant a permission the account does "
                "not have.",
                call,
            )

        ownership = self._check_ownership(call, state)
        if ownership is not None:
            return ownership

        # The row slice, checked after ownership so that "not yours" is still the answer
        # to someone else's order, and only then "yours, but not at this stage".
        stage = self._check_status_slice(call, state)
        if stage is not None:
            return stage

        # Acting for someone else needs a human decision on this exact argument set. It
        # is checked before the confirmation and never instead of it: an approval is one
        # more condition, not a way past the others.
        if state.acting_for_other and call.fingerprint not in state.approved_fingerprints:
            return GuardVerdict(
                Decision.NEEDS_APPROVAL,
                "This account is acting on a customer's behalf, so the action needs a "
                f"supervisor's approval before it can run:\n{call.describe()}",
                call,
            )

        if call.fingerprint in state.confirmed_fingerprints:
            return GuardVerdict(Decision.ALLOW, call=call)

        return GuardVerdict(
            Decision.NEEDS_CONFIRMATION,
            "This action changes order state and needs your explicit confirmation:\n"
            f"{call.describe()}",
            call,
        )

    def _check_read_scope(self, call: ProposedCall, state: SessionState) -> GuardVerdict:
        """Hold a read to the session's slice. Catalog reads are always allowed.

        This is the check the earlier stages did not make: a read used to pass on the
        `order:read` permission alone, so any authenticated account could read any
        order by id. The permission says *that* the account may read order data; the
        scope says *whose*.
        """
        scope = state.scope
        if not scope.enforce_reads or call.name in CATALOG_TOOLS:
            return GuardVerdict(Decision.ALLOW, call=call)

        if call.name in DISCOVERY_TOOLS:
            # Under an enforcing scope the identity came from a verified token, so a
            # lookup by email or by name and zip can only be aimed at somebody else.
            return self._refuse_slice(
                call,
                state,
                f"{call.name} looks up other accounts and is not available to this "
                "session: the authenticated identity is already known.",
            )

        for field_name in USER_ID_FIELDS:
            target = call.arguments.get(field_name)
            if target is not None and not scope.covers_user(str(target)):
                return self._refuse_slice(
                    call,
                    state,
                    f"This read targets user {target}, which is outside this session's "
                    "access scope.",
                )

        for field_name in ORDER_ID_FIELDS:
            order_id = call.arguments.get(field_name)
            if order_id is None:
                continue
            owner = self._owner_lookup(str(order_id))
            if owner is None:
                return self._refuse_slice(
                    call,
                    state,
                    f"Order {order_id} does not exist or its owner cannot be confirmed.",
                )
            if not scope.covers_user(owner):
                # Same wording whether the order belongs to somebody else or does not
                # exist, so a refused read cannot be used to enumerate order ids.
                return self._refuse_slice(
                    call,
                    state,
                    f"Order {order_id} does not exist or its owner cannot be confirmed.",
                )

        state.slice_audit.record(call.name, "allow", "within scope", state.scope.source)
        return GuardVerdict(Decision.ALLOW, call=call)

    def _check_status_slice(self, call: ProposedCall, state: SessionState) -> GuardVerdict | None:
        """Hold a write to the order lifecycle stages the scope names.

        Returns None when the scope names no stage, when no status can be looked up, or
        when the call passes. `covers_status` itself fails closed on an unknown status;
        the absence of a lookup is different from a lookup that answered "unknown".
        """
        if state.scope.statuses is None or self._status_lookup is None:
            return None
        order_id = call.arguments.get("order_id")
        if order_id is None:
            return None
        status = self._status_lookup(str(order_id))
        if state.scope.covers_status(status):
            return None
        allowed = ", ".join(sorted(state.scope.statuses))
        return self._refuse_slice(
            call,
            state,
            f"Order {order_id} is {status or 'of an undetermined status'}; this session "
            f"may only change orders in these states: {allowed}.",
        )

    def _refuse_slice(self, call: ProposedCall, state: SessionState, reason: str) -> GuardVerdict:
        """One slice refusal. Recording happens in `review`, which sees every refusal."""
        return GuardVerdict(Decision.DENIED_SLICE, reason, call)

    def _check_ownership(self, call: ProposedCall, state: SessionState) -> GuardVerdict | None:
        """A write may only touch resources owned by the session identity.

        Returns None when the call passes.
        """
        for field_name in USER_ID_FIELDS:
            target = call.arguments.get(field_name)
            if target is not None and str(target) != state.user_id:
                return GuardVerdict(
                    Decision.DENIED_OWNERSHIP,
                    f"This action targets user {target}, which does not match the "
                    f"authenticated user {state.user_id}. Refusing to run it.",
                    call,
                )

        for field_name in ORDER_ID_FIELDS:
            order_id = call.arguments.get(field_name)
            if order_id is None:
                continue
            owner = self._owner_lookup(str(order_id))
            if owner is None:
                return GuardVerdict(
                    Decision.DENIED_OWNERSHIP,
                    f"Order {order_id} does not exist or its owner cannot be "
                    "confirmed. Refusing to run a write action.",
                    call,
                )
            if owner != state.user_id:
                return GuardVerdict(
                    Decision.DENIED_OWNERSHIP,
                    f"Order {order_id} belongs to another user. The authenticated user "
                    f"{state.user_id} may not modify it.",
                    call,
                )
        return None
