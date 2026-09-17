"""Tests for slice-level access control: derived row slices and column projection.

Five layers, in the order a failure is cheapest to diagnose:

1. the slice function itself, against the real upstream records;
2. `AccessScope` as pure logic, with no guard around it;
3. `WriteGuard` under a scope - the refusals a reviewer will ask about;
4. the agent flow - what the model is actually shown after redaction;
5. the benchmark adapter - proof the default is still permissive, so `baseline-v3`
   remains comparable.

Layer 5 is the one that matters most and is easiest to forget: the read-scope check
lives in the same `WriteGuard` the benchmark shares, and turning it on by default would
silently invalidate every recorded evaluation result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from retail_support_agent.guardrails import Decision, ProposedCall, SessionState, WriteGuard
from retail_support_agent.scope import (
    REDACTED,
    AccessScope,
    slice_key,
)

DB_PATH = (
    Path(__file__).resolve().parents[1]
    / "third_party"
    / "tau2-bench"
    / "data"
    / "tau2"
    / "domains"
    / "retail"
    / "db.json"
)

KNOWN_TOOLS = {
    "get_order_details",
    "get_user_details",
    "get_product_details",
    "list_all_product_types",
    "find_user_id_by_email",
    "cancel_pending_order",
    "return_delivered_order_items",
    "modify_user_address",
}

OWNERS = {"#W001": "alice", "#W002": "bob"}
STATUSES = {"#W001": "pending", "#W002": "delivered"}


@pytest.fixture
def guard() -> WriteGuard:
    return WriteGuard(KNOWN_TOOLS, OWNERS.get, STATUSES.get)


@pytest.fixture
def scoped() -> SessionState:
    """A customer holding their own slice, with reads enforced."""
    return SessionState(user_id="alice", scope=AccessScope.for_user("alice"))


# ---- 1. the slice, derived from data that carries no slice marker ---------


class TestDerivedSliceKey:
    """The upstream dataset has no tenant, sensitivity or date column. These tests
    state what it *does* have, so a future data refresh that drops one of those fields
    fails here rather than silently widening everyone's access."""

    def test_slice_key_comes_from_fields_the_record_already_has(self) -> None:
        order = {
            "order_id": "#W2611340",
            "user_id": "james_li_5688",
            "status": "processed",
            "address": {"city": "New York", "state": "NY"},
        }
        assert slice_key(order) == ("james_li_5688", "processed", "NY")

    def test_a_missing_field_yields_an_empty_component_rather_than_raising(self) -> None:
        # An access check that raises mid-turn is harder to reason about than one that
        # produces a slice nothing matches.
        assert slice_key({}) == ("", "", "")
        assert slice_key({"user_id": "x", "status": "pending", "address": "not a mapping"}) == (
            "x",
            "pending",
            "",
        )

    @pytest.mark.skipif(not DB_PATH.is_file(), reason="upstream data not fetched")
    def test_every_upstream_order_yields_a_usable_slice(self) -> None:
        """The derivation is only a control if it works on all 1000 real records."""
        orders = json.loads(DB_PATH.read_text(encoding="utf-8"))["orders"]
        keys = [slice_key(order) for order in orders.values()]
        assert len(keys) == len(orders)
        assert all(user and status for user, status, _ in keys)
        assert {status for _, status, _ in keys} == {
            "pending",
            "processed",
            "delivered",
            "cancelled",
        }

    @pytest.mark.skipif(not DB_PATH.is_file(), reason="upstream data not fetched")
    def test_the_dataset_still_has_no_native_slice_column(self) -> None:
        """If upstream ever adds one, this fails and the derivation should be revisited
        rather than left in place out of habit."""
        orders = json.loads(DB_PATH.read_text(encoding="utf-8"))["orders"]
        present = {key for order in orders.values() for key in order}
        assert not present & {"tenant", "tenant_id", "sensitivity", "created_at", "region"}


# ---- 2. AccessScope as pure logic ----------------------------------------


class TestScopeLogic:
    def test_permissive_scope_covers_everyone(self) -> None:
        scope = AccessScope.permissive()
        assert scope.covers_user("anybody")
        assert scope.covers_status("cancelled")
        assert not scope.redacts

    def test_an_enforcing_scope_covers_only_its_principals(self) -> None:
        scope = AccessScope.for_user("alice")
        assert scope.covers_user("alice")
        assert not scope.covers_user("bob")

    def test_widening_returns_a_new_scope(self) -> None:
        """A released assignment must lose its access; widening in place would keep it."""
        base = AccessScope.for_user("agent_1")
        widened = base.including("customer_9", source="ticket:12")
        assert widened.covers_user("customer_9")
        assert not base.covers_user("customer_9")
        assert widened.source == "ticket:12"

    def test_an_unknown_status_fails_closed_when_a_status_slice_is_set(self) -> None:
        scope = AccessScope.for_user("alice", statuses=frozenset({"pending"}))
        assert scope.covers_status("pending")
        assert not scope.covers_status("delivered")
        assert not scope.covers_status(None)

    def test_no_status_slice_means_every_stage(self) -> None:
        assert AccessScope.for_user("alice").covers_status(None)


class TestColumnProjection:
    """The column slice needs no marker in the data: the field name is the label."""

    def test_a_hidden_field_is_replaced_at_any_depth(self) -> None:
        scope = AccessScope.for_user("alice", hidden_fields=frozenset({"email"}))
        payload = {"user_id": "alice", "email": "a@example.com", "kin": [{"email": "b@x.com"}]}
        result = json.loads(scope.project(json.dumps(payload)))
        assert result["email"] == REDACTED
        assert result["kin"][0]["email"] == REDACTED

    def test_the_key_stays_visible(self) -> None:
        """Dropping the key would make the model report the field as absent, which is a
        different answer from "present but not shown to me"."""
        scope = AccessScope.for_user("alice", hidden_fields=frozenset({"payment_methods"}))
        result = json.loads(scope.project(json.dumps({"payment_methods": {"card": "1234"}})))
        assert "payment_methods" in result

    def test_unhidden_fields_survive_untouched(self) -> None:
        scope = AccessScope.for_user("alice", hidden_fields=frozenset({"email"}))
        payload = {"order_id": "#W001", "items": [{"price": 47.84}]}
        assert json.loads(scope.project(json.dumps(payload))) == payload

    def test_non_json_output_passes_through(self) -> None:
        """Several upstream tools return a bare string. A parse failure must not turn a
        working read into an error."""
        scope = AccessScope.for_user("alice", hidden_fields=frozenset({"email"}))
        assert scope.project("Order cancelled successfully.") == "Order cancelled successfully."

    def test_no_hidden_fields_means_no_rewrite_at_all(self) -> None:
        raw = '{"email": "a@example.com"}'
        assert AccessScope.for_user("alice").project(raw) == raw


# ---- 3. the guard under a scope ------------------------------------------


class TestReadScope:
    def test_reading_another_users_order_is_refused(
        self, guard: WriteGuard, scoped: SessionState
    ) -> None:
        """The gap this stage closes: `order:read` used to be enough to read any order."""
        verdict = guard.review(ProposedCall("get_order_details", {"order_id": "#W002"}), scoped)
        assert verdict.decision is Decision.DENIED_SLICE

    def test_reading_your_own_order_is_allowed(
        self, guard: WriteGuard, scoped: SessionState
    ) -> None:
        verdict = guard.review(ProposedCall("get_order_details", {"order_id": "#W001"}), scoped)
        assert verdict.decision is Decision.ALLOW

    def test_reading_another_users_profile_is_refused(
        self, guard: WriteGuard, scoped: SessionState
    ) -> None:
        verdict = guard.review(ProposedCall("get_user_details", {"user_id": "bob"}), scoped)
        assert verdict.decision is Decision.DENIED_SLICE

    def test_catalog_reads_stay_open(self, guard: WriteGuard, scoped: SessionState) -> None:
        """Product data is identical for every account: slicing it protects nothing and
        denies the reads that make the domain answerable."""
        assert guard.review(ProposedCall("list_all_product_types", {}), scoped).allowed
        catalog = ProposedCall("get_product_details", {"product_id": "1"})
        assert guard.review(catalog, scoped).allowed

    def test_identity_lookup_is_refused_under_an_enforcing_scope(
        self, guard: WriteGuard, scoped: SessionState
    ) -> None:
        """With the identity already established by a token, a lookup by email can only
        be aimed at somebody else."""
        call = ProposedCall("find_user_id_by_email", {"email": "someone@example.com"})
        assert guard.review(call, scoped).decision is Decision.DENIED_SLICE

    def test_a_refused_read_does_not_say_whether_the_order_exists(
        self, guard: WriteGuard, scoped: SessionState
    ) -> None:
        """Different wording for "not yours" and "no such order" turns refusals into an
        order-id oracle."""
        foreign = guard.review(ProposedCall("get_order_details", {"order_id": "#W002"}), scoped)
        missing = guard.review(ProposedCall("get_order_details", {"order_id": "#W404"}), scoped)
        assert foreign.reason.replace("#W002", "X") == missing.reason.replace("#W404", "X")


class TestPermissiveDefaultIsUnchanged:
    """The default scope must reproduce the pre-slice behaviour exactly."""

    def test_reads_pass_without_a_scope(self, guard: WriteGuard) -> None:
        state = SessionState(user_id="alice")
        verdict = guard.review(ProposedCall("get_order_details", {"order_id": "#W002"}), state)
        assert verdict.decision is Decision.ALLOW

    def test_identity_lookup_passes_without_a_scope(self, guard: WriteGuard) -> None:
        state = SessionState(user_id="alice")
        call = ProposedCall("find_user_id_by_email", {"email": "someone@example.com"})
        assert guard.review(call, state).allowed

    def test_writes_are_still_owned_and_confirmed(self, guard: WriteGuard) -> None:
        state = SessionState(user_id="alice")
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W002"}), state)
        assert verdict.decision is Decision.DENIED_OWNERSHIP


class TestStatusSlice:
    def test_a_write_outside_the_stage_slice_is_refused(self, guard: WriteGuard) -> None:
        state = SessionState(
            user_id="alice",
            scope=AccessScope.for_user("alice", statuses=frozenset({"pending"})),
        )
        # #W001 is alice's and is pending, so only the stage can decide this one.
        pending = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W001"}), state)
        assert pending.decision is Decision.NEEDS_CONFIRMATION

        state.scope = AccessScope.for_user("alice", statuses=frozenset({"delivered"}))
        blocked = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W001"}), state)
        assert blocked.decision is Decision.DENIED_SLICE
        assert "delivered" in blocked.reason

    def test_ownership_is_answered_before_the_stage(self, guard: WriteGuard) -> None:
        """ "Not yours" must stay the answer to someone else's order, so that the stage
        of an order you cannot touch is never disclosed."""
        state = SessionState(
            user_id="alice",
            scope=AccessScope.for_user("alice", statuses=frozenset({"pending"})),
        )
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W002"}), state)
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_no_status_lookup_means_no_stage_check(self) -> None:
        """The benchmark adapter passes no status lookup and must not gain one by
        accident: it would cost a tool call the evaluation never budgeted for."""
        guard = WriteGuard(KNOWN_TOOLS, OWNERS.get)
        state = SessionState(
            user_id="alice",
            scope=AccessScope.for_user("alice", statuses=frozenset({"delivered"})),
        )
        verdict = guard.review(ProposedCall("cancel_pending_order", {"order_id": "#W001"}), state)
        assert verdict.decision is Decision.NEEDS_CONFIRMATION


class TestSliceAudit:
    def test_every_enforced_verdict_is_recorded_with_its_authority(
        self, guard: WriteGuard, scoped: SessionState
    ) -> None:
        guard.review(ProposedCall("get_order_details", {"order_id": "#W001"}), scoped)
        guard.review(ProposedCall("get_order_details", {"order_id": "#W002"}), scoped)
        verdicts = [row["verdict"] for row in scoped.slice_audit.rows]
        assert verdicts == ["allow", "deny"]
        assert {row["source"] for row in scoped.slice_audit.rows} == {"self"}

    def test_a_ticket_scope_records_which_case_authorized_it(self, guard: WriteGuard) -> None:
        state = SessionState(
            user_id="customer_9",
            scope=AccessScope.for_user("customer_9", source="ticket:12"),
        )
        guard.review(ProposedCall("get_user_details", {"user_id": "customer_9"}), state)
        assert state.slice_audit.rows[0]["source"] == "ticket:12"

    def test_the_permissive_default_records_nothing(self, guard: WriteGuard) -> None:
        state = SessionState(user_id="alice")
        guard.review(ProposedCall("get_order_details", {"order_id": "#W002"}), state)
        assert state.slice_audit.rows == []


# ---- 4. what the model is shown ------------------------------------------


class TestProjectionInTheAgentFlow:
    """Redaction is applied to the model's view. Tested through the agent rather than
    the scope alone, because the value of the control depends on where it is applied."""

    def test_the_model_never_sees_the_hidden_field(self) -> None:
        from retail_support_agent.agent import RetailAgent
        from test_agent_flow import FakeDomain, ScriptedModel, reply, tool_call

        class ProfileDomain(FakeDomain):
            def tool_names(self) -> set[str]:
                return {"get_user_details"}

            def execute(self, name: str, arguments: dict[str, Any]) -> Any:
                from retail_support_agent.domain import ToolResult

                return ToolResult(
                    name=name,
                    ok=True,
                    content=json.dumps({"user_id": "alice", "email": "alice@example.com"}),
                )

        model = ScriptedModel(
            [tool_call("get_user_details", {"user_id": "alice"}), reply("Here are your details.")]
        )
        state = SessionState(
            user_id="alice",
            scope=AccessScope.for_user("alice", hidden_fields=frozenset({"email"})),
        )
        agent = RetailAgent(model, ProfileDomain(), state)  # type: ignore[arg-type]
        agent.send("what is on my profile?")

        tool_messages = [
            message
            for conversation in model.seen_messages
            for message in conversation
            if message.get("role") == "tool"
        ]
        assert tool_messages, "the tool result never reached the model"
        assert all("alice@example.com" not in message["content"] for message in tool_messages)
        assert any(REDACTED in message["content"] for message in tool_messages)


# ---- 5. the benchmark adapter is untouched -------------------------------


class TestBenchmarkAdapterUnaffected:
    """`baseline-v3` was recorded with reads passing through. If that changes, the
    recorded results stop describing the code and the baseline must be re-run."""

    def test_the_benchmark_read_path_still_allows_an_unscoped_read(self) -> None:
        from retail_support_agent.bench_agent import GuardState, review_call

        guard_state = GuardState()
        verdict = review_call(
            guard_state, set(KNOWN_TOOLS), ProposedCall("get_order_details", {"order_id": "#W002"})
        )
        assert verdict.decision is Decision.ALLOW

    def test_the_benchmark_identity_lookup_still_runs(self) -> None:
        """It is how the benchmark establishes who the customer is; denying it would
        block every task at the first turn."""
        from retail_support_agent.bench_agent import GuardState, review_call

        verdict = review_call(
            GuardState(),
            set(KNOWN_TOOLS),
            ProposedCall("find_user_id_by_email", {"email": "a@example.com"}),
        )
        assert verdict.allowed
