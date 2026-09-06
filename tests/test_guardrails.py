"""写操作底线的测试：这是项目的安全核心，必须离线可测、不依赖模型。"""

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
        # 读别人的订单由领域政策管，守卫只拦写操作，不在这里越权
        assert verdict.decision is Decision.ALLOW

    def test_write_tool_requires_confirmation_first(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W001"}), state)
        assert verdict.decision is Decision.NEEDS_CONFIRMATION
        # 确认文案必须带上真实参数，不能只说"确认吗"
        assert "#W001" in verdict.reason
        assert "cancel_pending_order" in verdict.reason


class TestOwnership:
    def test_write_on_other_users_order_is_denied(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W002"}), state)
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_ownership_beats_confirmation(self, guard: WriteGuard, state: SessionState) -> None:
        """即使用户确认过，也不能操作别人的订单——归属校验优先于确认。"""
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
    """确认必须绑定到具体参数——这是"写操作确认绑定具体参数"的实现。"""

    def test_confirmed_call_is_allowed(self, guard: WriteGuard, state: SessionState) -> None:
        call = ProposedCall("cancel_pending_order", {"order_id": "#W001"})
        state.confirm(call)
        assert guard.review(call, state).decision is Decision.ALLOW

    def test_changing_arguments_invalidates_confirmation(
        self, guard: WriteGuard, state: SessionState
    ) -> None:
        state.confirm(ProposedCall("cancel_pending_order", {"order_id": "#W001"}))
        # 用户确认的是取消 #W001；换成退货就必须重新确认
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
        assert "工具调用" in (state.stop_reason() or "")

    def test_timeout_stops_session(self) -> None:
        state = SessionState(user_id="alice", limits=SessionLimits(timeout_seconds=0.0))
        assert "时限" in (state.stop_reason() or "")

    def test_fresh_session_can_proceed(self) -> None:
        assert SessionState(user_id="alice").stop_reason() is None
