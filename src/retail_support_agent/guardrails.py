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

# Argument names that may carry an order or user id. Used by the ownership check.
ORDER_ID_FIELDS = ("order_id",)
USER_ID_FIELDS = ("user_id",)


class Decision(StrEnum):
    """Verdict on one proposed tool call."""

    ALLOW = "allow"  # read action, or a write whose fingerprint is already confirmed
    NEEDS_CONFIRMATION = "needs_confirmation"  # write action, not confirmed for these arguments
    DENIED_OWNERSHIP = "denied_ownership"  # order does not belong to the session identity
    DENIED_PERMISSION = "denied_permission"  # the account's roles do not grant this action
    DENIED_UNKNOWN_TOOL = "denied_unknown_tool"  # not in the upstream tool list


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


@dataclass
class SessionState:
    """Per-session state. One session is processed serially."""

    user_id: str
    limits: SessionLimits = field(default_factory=SessionLimits)
    # What the account's roles allow (M3). The default is the customer set, which is
    # what the demo and evaluation paths run as; under authentication it is whatever
    # `role_memberships` grants, never what the token claims.
    permissions: frozenset[Permission] = DEFAULT_PERMISSIONS
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    tool_calls: int = 0
    # Fingerprints the user has confirmed. One confirmation covers one argument set.
    confirmed_fingerprints: set[str] = field(default_factory=set)
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
        return None

    def confirm(self, call: ProposedCall) -> None:
        """Record the user's confirmation of one specific argument set."""
        self.confirmed_fingerprints.add(call.fingerprint)
        self.pending = None


class WriteGuard:
    """Rules on every tool call. Does not rely on the model policing itself.

    Order of checks: known tool, then permission, then ownership, then confirmation.
    Each one is stricter than the last, and the reason given to the user is the first
    that fails, so a refusal never reveals more than the caller was entitled to ask.
    """

    def __init__(self, known_tools: set[str], owner_lookup: Any) -> None:
        """`owner_lookup(order_id) -> user_id | None`, supplied by RetailDomain."""
        self._known_tools = known_tools
        self._owner_lookup = owner_lookup

    def review(self, call: ProposedCall, state: SessionState) -> GuardVerdict:
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
            return GuardVerdict(Decision.ALLOW, call=call)

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

        if call.fingerprint in state.confirmed_fingerprints:
            return GuardVerdict(Decision.ALLOW, call=call)

        return GuardVerdict(
            Decision.NEEDS_CONFIRMATION,
            "This action changes order state and needs your explicit confirmation:\n"
            f"{call.describe()}",
            call,
        )

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
