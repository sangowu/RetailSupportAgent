"""Tests for the write-action limits: the safety core, testable offline without a model."""

from __future__ import annotations

import pytest

from retail_support_agent.guardrails import (
    Decision,
    ProposedCall,
    SessionLimits,
    SessionState,
    WriteGuard,
)

KNOWN_TOOLS = {
    "get_order_details",
    "cancel_pending_order",
    "return_delivered_order_items",
    "modify_user_address",
}

OWNERS = {"#W001": "alice", "#W002": "bob"}


@pytest.fixture
def guard() -> WriteGuard:
    return WriteGuard(KNOWN_TOOLS, lambda order_id: OWNERS.get(order_id))


@pytest.fixture
def state() -> SessionState:
    return SessionState(user_id="alice")


class TestReadVsWrite:
    def test_read_tool_passes_without_confirmation(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        verdict = guard.review(ProposedCall("get_order_details", {"order_id": "#W002"}), state)
        # Reading someone else's order is governed by the domain policy; the guard
        # covers writes only and does not overreach here.
        assert verdict.decision is Decision.ALLOW

    def test_write_tool_requires_confirmation_first(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W001"}), state)
        assert verdict.decision is Decision.NEEDS_CONFIRMATION
        # The confirmation text must carry the real arguments, not just "are you sure".
        assert "#W001" in verdict.reason
        assert "cancel_pending_order" in verdict.reason


class TestOwnership:
    def test_write_on_other_users_order_is_denied(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W002"}), state)
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_ownership_beats_confirmation(self, guard: WriteGuard, state: SessionState) -> None:
        """A confirmation does not authorize touching someone else's order: the
        ownership check comes first."""
        call = ProposedCall("cancel_pending_order", {"order_id": "#W002"})
        state.confirm(call)
        assert guard.review(call, state).decision is Decision.DENIED_OWNERSHIP

    def test_unknown_order_is_denied(self, guard: WriteGuard, state: SessionState) -> None:
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W999"}), state)
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_user_id_argument_must_match_session(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        verdict = guard.review(
            ProposedCall("modify_user_address", {"user_id": "bob", "city": "X"}), state
        )
        assert verdict.decision is Decision.DENIED_OWNERSHIP


class TestConfirmationBinding:
    """Confirmation is bound to specific arguments."""

    def test_confirmed_call_is_allowed(self, guard: WriteGuard, state: SessionState) -> None:
        call = ProposedCall("cancel_pending_order", {"order_id": "#W001"})
        state.confirm(call)
        assert guard.review(call, state).decision is Decision.ALLOW

    def test_changing_arguments_invalidates_confirmation(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        state.confirm(ProposedCall("cancel_pending_order", {"order_id": "#W001"}))
        # The user confirmed cancelling #W001; a return needs its own confirmation.
        swapped = ProposedCall("return_delivered_order_items", {"order_id": "#W001"})
        assert guard.review(swapped, state).decision is Decision.NEEDS_CONFIRMATION

    def test_extra_argument_invalidates_confirmation(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        state.confirm(ProposedCall("cancel_pending_order", {"order_id": "#W001"}))
        widened = ProposedCall("cancel_pending_order", {"order_id": "#W001", "reason": "no longer"})
        assert guard.review(widened, state).decision is Decision.NEEDS_CONFIRMATION

    def test_fingerprint_is_argument_order_independent(self) -> None:
        a = ProposedCall("x", {"p": 1, "q": 2})
        b = ProposedCall("x", {"q": 2, "p": 1})
        assert a.fingerprint == b.fingerprint


class TestUnknownTool:
    def test_tool_outside_domain_is_denied(self, guard: WriteGuard, state: SessionState) -> None:
        verdict = guard.review(ProposedCall("drop_database", {}), state)
        assert verdict.decision is Decision.DENIED_UNKNOWN_TOOL


class TestStopConditions:
    def test_step_limit_stops_session(self) -> None:
        state = SessionState(user_id="alice", limits=SessionLimits(max_steps=2))
        state.steps = 2
        assert state.stop_reason() is not None

    def test_tool_call_limit_stops_session(self) -> None:
        state = SessionState(user_id="alice", limits=SessionLimits(max_tool_calls=3))
        state.tool_calls = 3
        assert "tool-call limit" in (state.stop_reason() or "")

    def test_timeout_stops_session(self) -> None:
        state = SessionState(user_id="alice", limits=SessionLimits(timeout_seconds=0.0))
        assert "time limit" in (state.stop_reason() or "")

    def test_fresh_session_can_proceed(self) -> None:
        assert SessionState(user_id="alice").stop_reason() is None


class TestRefusedActionsAreBounded:
    """A session that keeps reaching for things it may not have is probing, and the
    refusals alone do not stop it: each one is correct and the next one is free. What
    this bounds is how many a single session may collect.
    """

    def test_repeated_refusals_stop_the_session(self, guard: WriteGuard) -> None:
        state = SessionState(user_id="alice", limits=SessionLimits(max_denials=3))
        for _ in range(3):
            call = ProposedCall("cancel_pending_order", {"order_id": "#W002"})
            assert guard.review(call, state).decision is Decision.DENIED_OWNERSHIP
        assert state.denials == 3
        assert "refused-action limit" in (state.stop_reason() or "")

    def test_one_refusal_does_not(self, guard: WriteGuard, state: SessionState) -> None:
        guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W002"}), state)
        assert state.stop_reason() is None

    def test_confirmations_are_not_refusals(self, guard: WriteGuard) -> None:
        """Asking the user to confirm is the system working, not a probe. Counting it
        would end every careful conversation at the fifth write."""
        state = SessionState(user_id="alice", limits=SessionLimits(max_denials=3))
        for _ in range(5):
            verdict = guard.review(
                ProposedCall("cancel_pending_order", {"order_id": "#W001"}), state
            )
            assert verdict.decision is Decision.NEEDS_CONFIRMATION
        assert state.denials == 0
        assert state.stop_reason() is None

    def test_an_invented_tool_name_is_not_a_probe(self, guard: WriteGuard) -> None:
        """A model that made up a tool name is a model making a mistake. Ending the
        session over it would punish the user for the model's error."""
        state = SessionState(user_id="alice", limits=SessionLimits(max_denials=2))
        for _ in range(4):
            guard.review(ProposedCall("delete_everything", {}), state)
        assert state.denials == 0

    def test_a_permission_refusal_counts_too(self) -> None:
        """Whether the account lacks the permission or the resource is someone else's,
        a session collecting refusals is the same signal."""
        from retail_support_agent.auth import Permission

        state = SessionState(
            user_id="alice",
            limits=SessionLimits(max_denials=2),
            permissions=frozenset({Permission.ORDER_READ}),
        )
        guard = WriteGuard(KNOWN_TOOLS, OWNERS.get)
        for _ in range(2):
            verdict = guard.review(
                ProposedCall("cancel_pending_order", {"order_id": "#W001"}), state
            )
            assert verdict.decision is Decision.DENIED_PERMISSION
        assert state.stop_reason() is not None
