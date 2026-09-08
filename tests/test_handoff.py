"""M4: handoff tickets, assignment, approval, and the timing split.

One class per acceptance threshold in docs/M4_HANDOFF.md §1.2.

Most of these need a real PostgreSQL: one open ticket per session and one active
assignment per ticket are partial unique indexes, and approval expiry is compared by the
database clock. With no database they skip rather than pass vacuously. Locally:
`docker compose up -d db`, then point `RSA_TEST_DATABASE_URL` at it.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from retail_support_agent.agent import RetailAgent
from retail_support_agent.api import create_app
from retail_support_agent.auth import AuthConfig, Identity, Permission, StaticKeys
from retail_support_agent.config import Settings
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.domain import ToolResult
from retail_support_agent.guardrails import (
    Decision,
    ProposedCall,
    SessionLimits,
    SessionState,
    WriteGuard,
)
from retail_support_agent.store import Store

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

CUSTOMER = "ava_moore_2033"
CANCEL = ProposedCall("cancel_pending_order", {"order_id": "#W001", "reason": "no longer needed"})
OTHER_CANCEL = ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "changed mind"})


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    if not TEST_URL:
        if os.getenv("CI"):
            raise RuntimeError(
                "CI must provide RSA_TEST_DATABASE_URL for the job running these tests. "
                "Skipping them means none of the M4 thresholds ran while CI still reports "
                "green."
            )
        pytest.skip("needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (docker compose up -d db)")
    engine = make_engine(TEST_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


def new_session(store: Store, subject: str | None = None) -> str:
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    store.ensure_session(session_id, CUSTOMER, subject)
    return session_id


def new_subject(prefix: str = "sub") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---- doubles ------------------------------------------------------------


class HandoffDomain:
    """A domain with one write and the transfer tool, so a turn can be driven offline."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.transfers: list[str] = []

    @property
    def policy(self) -> str:
        return "(test policy)"

    def tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def tool_names(self) -> set[str]:
        return {"get_order_details", "cancel_pending_order", "transfer_to_human_agents"}

    def order_owner(self, order_id: str) -> str | None:
        return CUSTOMER

    def db_hash(self) -> str:
        return json.dumps(sorted(self.cancelled))

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "cancel_pending_order":
            self.cancelled.append(str(arguments["order_id"]))
        if name == "transfer_to_human_agents":
            self.transfers.append(str(arguments.get("summary", "")))
            return ToolResult(name=name, ok=True, content="Transfer successful")
        return ToolResult(name=name, ok=True, content=json.dumps({"status": "cancelled"}))


class ScriptedModel:
    """Replays prepared responses and counts how often it was called.

    The count is the point in several tests below: a suspended session must not reach the
    model at all, and "it produced no tool calls" would not prove that.
    """

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self.calls += 1
        if not self.responses:
            return _message(content="(nothing further)")
        return self.responses.pop(0)


class _Message:
    def __init__(self, content: str | None, tool_calls: list[Any] | None) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Response:
    def __init__(self, message: _Message) -> None:
        self.choices = [_Choice(message)]


class _Call:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.id = f"call-{uuid.uuid4().hex[:8]}"
        self.type = "function"
        self.function = _Function(name, arguments)


class _Function:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = json.dumps(arguments)


def _message(content: str | None = None, calls: list[_Call] | None = None) -> _Response:
    return _Response(_Message(content, calls))


def tool_call(name: str, **arguments: Any) -> _Call:
    return _Call(name, arguments)


def build_agent(
    store: Store | None, session_id: str, domain: HandoffDomain, model: ScriptedModel
) -> RetailAgent:
    state = SessionState(user_id=CUSTOMER, limits=SessionLimits())
    return RetailAgent(
        model,
        domain,  # type: ignore[arg-type]
        state,
        store=store,
        session_id=session_id,
    )


def guard_for(domain: HandoffDomain) -> WriteGuard:
    return WriteGuard(domain.tool_names(), domain.order_owner)


# ---- 1. escalation ------------------------------------------------------


class TestEscalationCreatesATicket:
    """Threshold: a handoff creates exactly one open ticket with session, requester and
    summary."""

    def test_a_transfer_opens_a_ticket(self, store: Store) -> None:
        session_id = new_session(store)
        domain = HandoffDomain()
        model = ScriptedModel(
            _message(calls=[tool_call("transfer_to_human_agents", summary="wants a refund")])
        )
        agent = build_agent(store, session_id, domain, model)
        agent.apply_context(Identity("s", CUSTOMER, frozenset({"customer"})).permissions, "s")

        result = agent.send("I want to speak to a person")

        assert result.status == "handed_off"
        ticket = store.open_ticket_for(session_id)
        assert ticket is not None
        assert ticket.state == "open"
        assert ticket.user_id == CUSTOMER
        assert ticket.summary == "wants a refund"
        assert ticket.requested_by == "s"
        assert f"#{ticket.id}" in result.reply

    def test_asking_twice_is_the_same_case(self, store: Store) -> None:
        """A second transfer on a waiting session is the same case, not a new one. Two
        open tickets would make "is this session suspended" ambiguous."""
        session_id = new_session(store)
        first = store.open_ticket(session_id, CUSTOMER, "first", "s")
        again = store.open_ticket(session_id, CUSTOMER, "second", "s")
        assert first.id == again.id
        assert len(store.tickets()) >= 1

    def test_a_ticket_records_who_asked(self, store: Store) -> None:
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "summary", "sub-abc")
        assert ticket.requested_by == "sub-abc"


# ---- 2. suspension ------------------------------------------------------


class TestTheSessionIsSuspendedWhileAHumanHoldsIt:
    """Threshold: an open ticket stops the session running tools or calling the model."""

    def test_no_model_call_while_a_ticket_is_open(self, store: Store) -> None:
        session_id = new_session(store)
        store.open_ticket(session_id, CUSTOMER, "waiting", None)
        model = ScriptedModel(_message(content="the model should never be reached"))
        agent = build_agent(store, session_id, HandoffDomain(), model)

        result = agent.send("are you still there?")

        assert result.status == "waiting_for_human"
        assert model.calls == 0, "a suspended session must bill nothing"
        assert result.tool_calls == []

    def test_what_the_customer_says_while_waiting_is_kept(self, store: Store) -> None:
        """The human taking the case needs to see it; dropping it would lose part of the
        conversation the ticket is about."""
        session_id = new_session(store)
        store.open_ticket(session_id, CUSTOMER, "waiting", None)
        agent = build_agent(store, session_id, HandoffDomain(), ScriptedModel())

        agent.send("one more thing: my order number is #W001")

        contents = [row.content for row in store.messages(session_id)]
        assert "one more thing: my order number is #W001" in contents

    def test_the_agent_working_the_case_is_not_blocked_by_it(self, store: Store) -> None:
        """The ticket suspends the customer's session, not the assignee's."""
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "waiting", None)
        model = ScriptedModel(_message(content="looking into it"))
        agent = build_agent(store, session_id, HandoffDomain(), model)
        agent.apply_context(
            Identity("agent-1", CUSTOMER, frozenset({"agent"})).permissions,
            "agent-1",
            ticket_id=ticket.id,
        )

        result = agent.send("checking the order")

        assert result.status == "answered"
        assert model.calls == 1


# ---- 3. continuation ----------------------------------------------------


class TestContinuationAfterAResolution:
    """Threshold: closing the ticket resumes the session with its state intact."""

    def test_the_session_runs_again_once_the_ticket_is_closed(self, store: Store) -> None:
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "waiting", None)
        model = ScriptedModel(_message(content="the refund is on its way"))
        agent = build_agent(store, session_id, HandoffDomain(), model)
        assert agent.send("hello?").status == "waiting_for_human"

        assert store.close_ticket(ticket.id, "resolved", "refunded by hand")

        result = agent.send("hello?")
        assert result.status == "answered"
        assert model.calls == 1

    def test_the_counters_and_conversation_survive_the_wait(self, store: Store) -> None:
        """Suspension is not a reset: M2's guarantees still hold across a handoff."""
        session_id = new_session(store)
        agent = build_agent(store, session_id, HandoffDomain(), ScriptedModel())
        store.save_progress(session_id, steps=7, tool_calls=5)
        ticket = store.open_ticket(session_id, CUSTOMER, "waiting", None)
        agent.send("waiting")
        store.close_ticket(ticket.id, "resolved", "done")

        progress = store.progress(session_id)
        assert progress is not None
        assert (progress.steps, progress.tool_calls) == (7, 5)

    def test_a_closed_ticket_releases_its_assignment(self, store: Store) -> None:
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "waiting", None)
        store.assign_ticket(ticket.id, "agent-1", "boss")
        assert store.assignee_of(ticket.id) == "agent-1"

        store.close_ticket(ticket.id, "resolved", "done")
        assert store.assignee_of(ticket.id) is None

    def test_closing_twice_is_refused(self, store: Store) -> None:
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "waiting", None)
        assert store.close_ticket(ticket.id, "resolved", "done")
        assert not store.close_ticket(ticket.id, "rejected", "changed my mind")


# ---- 4. refusal and approval --------------------------------------------


class TestActingForACustomerNeedsAHumanDecision:
    """Threshold: a rejected escalation leaves the action unexecuted."""

    def test_an_unapproved_action_is_held(self) -> None:
        state = SessionState(user_id=CUSTOMER, acting_for_other=True)
        state.confirm(CANCEL)  # the customer's confirmation is not enough on its own
        verdict = guard_for(HandoffDomain()).review(CANCEL, state)
        assert verdict.decision is Decision.NEEDS_APPROVAL

    def test_the_request_is_recorded_for_a_human_to_decide(self, store: Store) -> None:
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "case", None)
        domain = HandoffDomain()
        model = ScriptedModel(
            _message(
                calls=[
                    tool_call("cancel_pending_order", order_id="#W001", reason="no longer needed")
                ]
            )
        )
        agent = build_agent(store, session_id, domain, model)
        agent.apply_context(
            Identity("agent-1", CUSTOMER, frozenset({"agent"})).permissions,
            "agent-1",
            ticket_id=ticket.id,
        )
        agent._session.confirm(CANCEL)  # the customer already agreed to these arguments

        result = agent.send("cancel it for them")

        assert result.status == "needs_approval"
        assert domain.cancelled == [], "nothing may run before the decision"
        pending = store.pending_approvals(session_id)
        assert [row.fingerprint for row in pending] == [CANCEL.fingerprint]
        assert pending[0].requested_by == "agent-1"

    def test_a_rejection_leaves_the_action_unexecuted(self, store: Store) -> None:
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        assert store.decide_approval(approval_id, approve=False, decided_by="boss") == "rejected"

        assert store.approved_fingerprints(session_id) == set()
        state = SessionState(user_id=CUSTOMER, acting_for_other=True)
        state.confirm(CANCEL)
        state.approved_fingerprints = store.approved_fingerprints(session_id)
        assert guard_for(HandoffDomain()).review(CANCEL, state).decision is Decision.NEEDS_APPROVAL
        assert store.operations(session_id) == []

    def test_an_approval_lets_the_same_arguments_through(self, store: Store) -> None:
        """The positive control: without it, a refusal of everything would also pass the
        tests above."""
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        assert store.decide_approval(approval_id, approve=True, decided_by="boss") == "approved"

        state = SessionState(user_id=CUSTOMER, acting_for_other=True)
        state.confirm(CANCEL)
        state.approved_fingerprints = store.approved_fingerprints(session_id)
        assert guard_for(HandoffDomain()).review(CANCEL, state).allowed

    def test_deciding_twice_is_refused(self, store: Store) -> None:
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        store.decide_approval(approval_id, approve=False, decided_by="boss")
        assert (
            store.decide_approval(approval_id, approve=True, decided_by="boss") == "already_decided"
        )

    def test_the_same_request_is_not_duplicated(self, store: Store) -> None:
        """A model proposing the same call twice must not fill a queue with copies of one
        decision."""
        session_id = new_session(store)
        first = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        again = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        assert first == again


class TestApprovalIsNotABypass:
    """Threshold: an approved action still fails every other check it should."""

    def test_it_does_not_replace_the_customers_confirmation(self, store: Store) -> None:
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        store.decide_approval(approval_id, approve=True, decided_by="boss")

        state = SessionState(user_id=CUSTOMER, acting_for_other=True)
        state.approved_fingerprints = store.approved_fingerprints(session_id)
        assert (
            guard_for(HandoffDomain()).review(CANCEL, state).decision is Decision.NEEDS_CONFIRMATION
        )

    def test_it_does_not_reach_another_users_order(self, store: Store) -> None:
        session_id = new_session(store)
        foreign = ProposedCall("cancel_pending_order", {"order_id": "#W999", "reason": "no"})
        approval_id = store.open_approval(session_id, foreign, requested_by="agent-1")
        store.decide_approval(approval_id, approve=True, decided_by="boss")

        class ForeignDomain(HandoffDomain):
            def order_owner(self, order_id: str) -> str | None:
                return "someone_else_5150"

        state = SessionState(user_id=CUSTOMER, acting_for_other=True)
        state.confirm(foreign)
        state.approved_fingerprints = store.approved_fingerprints(session_id)
        verdict = guard_for(ForeignDomain()).review(foreign, state)
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_it_covers_one_argument_set_only(self, store: Store) -> None:
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        store.decide_approval(approval_id, approve=True, decided_by="boss")

        state = SessionState(user_id=CUSTOMER, acting_for_other=True)
        state.confirm(OTHER_CANCEL)
        state.approved_fingerprints = store.approved_fingerprints(session_id)
        assert (
            guard_for(HandoffDomain()).review(OTHER_CANCEL, state).decision
            is Decision.NEEDS_APPROVAL
        )

    def test_it_does_not_give_a_role_a_permission(self, store: Store) -> None:
        """An approval says a specific action may run, not that the account may write."""
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="auditor-1")
        store.decide_approval(approval_id, approve=True, decided_by="boss")

        auditor = Identity("auditor-1", CUSTOMER, frozenset({"auditor"}))
        state = SessionState(
            user_id=CUSTOMER, permissions=auditor.permissions, acting_for_other=True
        )
        state.confirm(CANCEL)
        state.approved_fingerprints = store.approved_fingerprints(session_id)
        assert (
            guard_for(HandoffDomain()).review(CANCEL, state).decision is Decision.DENIED_PERMISSION
        )


class TestApprovalExpiry:
    """Threshold: a decision older than its TTL authorizes nothing."""

    def test_an_expired_approval_authorizes_nothing(self, store: Store) -> None:
        session_id = new_session(store)
        # A negative TTL puts expires_at in the past by the database's own clock, which
        # is how a real one gets there: nobody used it in time.
        approval_id = store.open_approval(
            session_id, CANCEL, requested_by="agent-1", ttl_seconds=-1.0
        )
        store.decide_approval(approval_id, approve=True, decided_by="boss")
        assert store.approved_fingerprints(session_id) == set()

    def test_an_expired_request_cannot_be_decided(self, store: Store) -> None:
        session_id = new_session(store)
        approval_id = store.open_approval(
            session_id, CANCEL, requested_by="agent-1", ttl_seconds=-1.0
        )
        assert store.decide_approval(approval_id, approve=True, decided_by="boss") == "expired"

    def test_an_expired_request_is_not_listed_as_pending(self, store: Store) -> None:
        session_id = new_session(store)
        store.open_approval(session_id, CANCEL, requested_by="agent-1", ttl_seconds=-1.0)
        assert store.pending_approvals(session_id) == []

    def test_the_default_validity_is_thirty_minutes(self) -> None:
        """The architecture document fixes the default; a silent change here would make
        an authorization outlive what was agreed."""
        from retail_support_agent.store import APPROVAL_TTL_SECONDS

        assert APPROVAL_TTL_SECONDS == 30 * 60

    def test_the_validity_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ "Configurable and covered by tests" is the architecture document's wording, so
        the environment variable is part of the requirement rather than a convenience."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        monkeypatch.setenv("RSA_APPROVAL_TTL_SECONDS", "60")
        assert Settings(_env_file=None).approval_ttl_seconds == 60.0  # type: ignore[call-arg]

    def test_a_shorter_window_is_applied_to_the_request(self, store: Store) -> None:
        session_id = new_session(store)
        approval_id = store.open_approval(
            session_id, CANCEL, requested_by="agent-1", ttl_seconds=0.5
        )
        row = store.approval(approval_id)
        assert row is not None
        assert (row.expires_at - row.created_at).total_seconds() == pytest.approx(0.5, abs=0.05)


# ---- 5. nobody decides their own ----------------------------------------


class TestNoSelfDecision:
    """Threshold: the requester cannot approve their own escalation."""

    def test_the_requester_is_refused(self, store: Store) -> None:
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="agent-1")
        assert (
            store.decide_approval(approval_id, approve=True, decided_by="agent-1")
            == "self_decision"
        )
        assert store.approved_fingerprints(session_id) == set()

    def test_the_rule_lives_in_the_store_not_only_the_api(self, store: Store) -> None:
        """Checked through the store directly, so a second entry point cannot skip it."""
        session_id = new_session(store)
        approval_id = store.open_approval(session_id, CANCEL, requested_by="x")
        assert store.decide_approval(approval_id, approve=False, decided_by="x") == "self_decision"
        row = store.approval(approval_id)
        assert row is not None and row.state == "pending"


# ---- 6. assignment scope ------------------------------------------------


class TestAssignmentScope:
    """Threshold: an agent reaches the assigned customer's orders and no others."""

    def test_reassignment_releases_the_previous_holder(self, store: Store) -> None:
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "case", None)
        store.assign_ticket(ticket.id, "agent-1", "boss")
        store.assign_ticket(ticket.id, "agent-2", "boss")

        assert store.assignee_of(ticket.id) == "agent-2"
        # The history survives: who could see the customer's orders, and when, stays
        # readable after a reassignment.
        assert [t.id for t in store.tickets(assignee="agent-1")] == []
        assert [t.id for t in store.tickets(assignee="agent-2")] == [ticket.id]

    def test_an_agent_sees_only_their_own_queue(self, store: Store) -> None:
        mine = store.open_ticket(new_session(store), CUSTOMER, "mine", None)
        theirs = store.open_ticket(new_session(store), CUSTOMER, "theirs", None)
        store.assign_ticket(mine.id, "agent-1", "boss")
        store.assign_ticket(theirs.id, "agent-2", "boss")

        ids = [t.id for t in store.tickets(assignee="agent-1")]
        assert mine.id in ids
        assert theirs.id not in ids

    def test_a_closed_ticket_cannot_be_assigned(self, store: Store) -> None:
        ticket = store.open_ticket(new_session(store), CUSTOMER, "case", None)
        store.close_ticket(ticket.id, "resolved", "done")
        assert not store.assign_ticket(ticket.id, "agent-1", "boss")


class TestTheRoleMatrixHasTeethNow:
    """The M3 note said `agent` gained nothing until assignment existed. It does now."""

    def test_an_agent_can_read_and_work_tickets(self) -> None:
        agent = Identity("a", CUSTOMER, frozenset({"agent"}))
        assert agent.may(Permission.TICKET_READ)
        assert agent.may(Permission.TICKET_WORK)

    def test_an_agent_cannot_assign_or_decide(self) -> None:
        agent = Identity("a", CUSTOMER, frozenset({"agent"}))
        assert not agent.may(Permission.TICKET_ASSIGN)
        assert not agent.may(Permission.ESCALATION_DECIDE)

    def test_a_supervisor_decides_but_does_not_work_the_case(self) -> None:
        """An approver who also performs the action is not an approver."""
        supervisor = Identity("s", CUSTOMER, frozenset({"supervisor"}))
        assert supervisor.may(Permission.TICKET_ASSIGN)
        assert supervisor.may(Permission.ESCALATION_DECIDE)
        assert not supervisor.may(Permission.TICKET_WORK)

    def test_a_customer_has_no_ticket_powers(self) -> None:
        customer = Identity("c", CUSTOMER, frozenset({"customer"}))
        assert not customer.may(Permission.TICKET_READ)
        assert not customer.may(Permission.TICKET_WORK)


# ---- 7. the timing split ------------------------------------------------


class TestTimingIsSplit:
    """Threshold: system time and human waiting are two numbers, not one."""

    def test_an_open_ticket_counts_as_waiting(self, store: Store) -> None:
        session_id = new_session(store)
        store.open_ticket(session_id, CUSTOMER, "waiting", None)
        timing = store.session_timing(session_id)
        assert timing.waiting_seconds > 0
        assert timing.system_seconds == pytest.approx(
            timing.total_seconds - timing.waiting_seconds, abs=1e-6
        )

    def test_a_session_with_no_ticket_waits_for_nobody(self, store: Store) -> None:
        session_id = new_session(store)
        timing = store.session_timing(session_id)
        assert timing.waiting_seconds == 0.0
        assert timing.system_seconds == timing.total_seconds

    def test_overlapping_waits_are_counted_once(self, store: Store) -> None:
        """A ticket open while an approval is pending is one wait. Summing both would
        report more waiting than the session has existed."""
        session_id = new_session(store)
        ticket = store.open_ticket(session_id, CUSTOMER, "waiting", None)
        store.open_approval(session_id, CANCEL, requested_by="agent-1", ticket_id=ticket.id)
        timing = store.session_timing(session_id)
        assert timing.waiting_seconds <= timing.total_seconds
        assert timing.system_seconds >= 0.0

    def test_an_unknown_session_reports_zero_rather_than_failing(self, store: Store) -> None:
        timing = store.session_timing("never-existed")
        assert (timing.total_seconds, timing.waiting_seconds) == (0.0, 0.0)


# ---- 8. degradation and the benchmark path ------------------------------


class TestWithoutADatabase:
    """Threshold: with no database, handoff behaves as it did before M4."""

    def test_a_transfer_still_answers(self) -> None:
        domain = HandoffDomain()
        model = ScriptedModel(
            _message(calls=[tool_call("transfer_to_human_agents", summary="needs a human")])
        )
        agent = build_agent(None, "s-nodb", domain, model)
        result = agent.send("get me a person")

        assert result.status == "answered"
        assert result.reply == "Handed off to a human agent."
        assert domain.transfers == ["needs a human"]

    def test_nothing_suspends_a_session_that_has_no_tickets(self) -> None:
        model = ScriptedModel(_message(content="sure"))
        agent = build_agent(None, "s-nodb-2", HandoffDomain(), model)
        assert agent.send("hello").status == "answered"
        assert model.calls == 1


class TestTheBenchmarkPathIsUnchanged:
    """The evaluation stack must not move: `baseline-v3` was measured with these rules."""

    def test_a_default_session_never_needs_approval(self) -> None:
        """`acting_for_other` is False unless a ticket says otherwise, so the benchmark
        path reaches exactly the M3 verdicts."""
        state = SessionState(user_id=CUSTOMER)
        assert state.acting_for_other is False
        state.confirm(CANCEL)
        assert guard_for(HandoffDomain()).review(CANCEL, state).allowed

    def test_the_transfer_tool_is_still_an_ordinary_read(self) -> None:
        """Upstream treats `transfer_to_human_agents` as a normal tool call. Making it a
        write, or attaching a permission to it, would change what the benchmark measures.
        """
        from retail_support_agent.domain import WRITE_TOOLS

        assert "transfer_to_human_agents" not in WRITE_TOOLS
        state = SessionState(user_id=CUSTOMER)
        call = ProposedCall("transfer_to_human_agents", {"summary": "needs a human"})
        assert guard_for(HandoffDomain()).review(call, state).allowed


# ---- 9. the API surface -------------------------------------------------


@pytest.fixture
def client(store: Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The service with authentication off, so these tests exercise the ticket routes
    rather than re-testing M3's verifier. Permissions are still enforced: the demo
    identity holds the `demo` role, which has no ticket powers at all."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    with TestClient(create_app(settings=Settings(), store=store)) as test_client:  # type: ignore[call-arg]
        yield test_client


class TestTicketRoutesRefuseTheWrongRole:
    def test_the_demo_identity_cannot_read_or_assign_tickets(self, client: TestClient) -> None:
        assert client.get("/tickets").status_code == 403
        assert client.post("/tickets/1/assign", json={"assignee": "agent-1"}).status_code == 403
        assert client.get("/approvals").status_code == 403

    def test_health_reports_the_milestone(self, client: TestClient) -> None:
        assert client.get("/health").json()["milestone"] == "M4"


# ---- 10. the ticket routes, with authentication on ----------------------

# The JWT helpers live in the M3 test file. Reusing them keeps one definition of what a
# valid token looks like: a second copy would drift from the verifier the moment either
# changed.
from test_auth import AUDIENCE, ISSUER, Keys, bearer, mint  # noqa: E402


@pytest.fixture(scope="module")
def keys() -> Keys:
    from test_auth import _make_keys

    return _make_keys()


@pytest.fixture(scope="module")
def auth_config(keys: Keys) -> AuthConfig:
    return AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))


@pytest.fixture
def secure_client(
    store: Store, auth_config: AuthConfig, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    app = create_app(settings=Settings(), store=store, auth=auth_config)  # type: ignore[call-arg]
    with TestClient(app) as test_client:
        yield test_client


class Cast:
    """One supervisor, two agents and a customer, provisioned and holding a ticket."""

    def __init__(self, store: Store, keys: Keys) -> None:
        self.supervisor = new_subject("boss")
        self.agent = new_subject("agent")
        self.other_agent = new_subject("agent")
        self.customer = new_subject("cust")
        store.provision_identity(self.supervisor, CUSTOMER, ["supervisor"])
        store.provision_identity(self.agent, CUSTOMER, ["agent"])
        store.provision_identity(self.other_agent, CUSTOMER, ["agent"])
        store.provision_identity(self.customer, CUSTOMER, ["customer"])
        self.session_id = new_session(store, self.customer)
        self.ticket = store.open_ticket(self.session_id, CUSTOMER, "needs a person", self.customer)
        self.keys = keys

    def token(self, subject: str) -> dict[str, str]:
        return bearer(mint(self.keys, subject=subject))


@pytest.fixture
def cast(store: Store, keys: Keys) -> Cast:
    return Cast(store, keys)


class TestAssignmentThroughTheApi:
    def test_a_supervisor_assigns_and_an_agent_sees_only_their_queue(
        self, secure_client: TestClient, cast: Cast
    ) -> None:
        assigned = secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.agent},
            headers=cast.token(cast.supervisor),
        )
        assert assigned.status_code == 200
        assert assigned.json()["assignee"] == cast.agent

        mine = secure_client.get("/tickets", headers=cast.token(cast.agent)).json()
        assert [t["id"] for t in mine] == [cast.ticket.id]
        theirs = secure_client.get("/tickets", headers=cast.token(cast.other_agent)).json()
        assert theirs == []

    def test_a_supervisor_cannot_assign_a_case_to_themselves(
        self, secure_client: TestClient, cast: Cast
    ) -> None:
        """Assigning and deciding are this role's powers; doing the work as well would
        leave the decision with nobody to check it."""
        response = secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.supervisor},
            headers=cast.token(cast.supervisor),
        )
        assert response.status_code == 403

    def test_an_agent_cannot_assign_at_all(self, secure_client: TestClient, cast: Cast) -> None:
        response = secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.agent},
            headers=cast.token(cast.agent),
        )
        assert response.status_code == 403

    def test_a_customer_cannot_read_tickets(self, secure_client: TestClient, cast: Cast) -> None:
        assert secure_client.get("/tickets", headers=cast.token(cast.customer)).status_code == 403

    def test_assigning_to_an_unprovisioned_subject_is_refused(
        self, secure_client: TestClient, cast: Cast
    ) -> None:
        response = secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": "nobody-here"},
            headers=cast.token(cast.supervisor),
        )
        assert response.status_code == 404


class TestWorkingATicket:
    def test_the_assignee_gets_a_session_bound_to_the_ticket(
        self, secure_client: TestClient, cast: Cast, store: Store
    ) -> None:
        secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.agent},
            headers=cast.token(cast.supervisor),
        )
        response = secure_client.post(
            f"/tickets/{cast.ticket.id}/session", headers=cast.token(cast.agent)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ticket_id"] == cast.ticket.id
        assert body["user_id"] == CUSTOMER
        # The agent works in their own session, not the customer's: the customer's
        # conversation belongs to the customer.
        assert body["session_id"] != cast.session_id
        assert store.session_ticket(body["session_id"]) == cast.ticket.id

    def test_another_agent_cannot_open_that_session(
        self, secure_client: TestClient, cast: Cast
    ) -> None:
        secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.agent},
            headers=cast.token(cast.supervisor),
        )
        response = secure_client.post(
            f"/tickets/{cast.ticket.id}/session", headers=cast.token(cast.other_agent)
        )
        assert response.status_code == 403

    def test_reassignment_ends_the_previous_agents_access(
        self, secure_client: TestClient, cast: Cast
    ) -> None:
        """The access is re-read every turn, so a reassignment takes effect at once
        rather than when the process happens to restart."""
        secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.agent},
            headers=cast.token(cast.supervisor),
        )
        session_id = secure_client.post(
            f"/tickets/{cast.ticket.id}/session", headers=cast.token(cast.agent)
        ).json()["session_id"]

        secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.other_agent},
            headers=cast.token(cast.supervisor),
        )
        response = secure_client.post(
            "/chat",
            json={"session_id": session_id, "message": "continuing the case"},
            headers=cast.token(cast.agent),
        )
        # 403 before the model is called: the refusal is about access, not about the
        # request being malformed.
        assert response.status_code == 403

    def test_a_supervisor_may_not_work_a_case(self, secure_client: TestClient, cast: Cast) -> None:
        secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.agent},
            headers=cast.token(cast.supervisor),
        )
        response = secure_client.post(
            f"/tickets/{cast.ticket.id}/session", headers=cast.token(cast.supervisor)
        )
        assert response.status_code == 403

    def test_closing_a_case_is_refused_to_a_third_party(
        self, secure_client: TestClient, cast: Cast
    ) -> None:
        secure_client.post(
            f"/tickets/{cast.ticket.id}/assign",
            json={"assignee": cast.agent},
            headers=cast.token(cast.supervisor),
        )
        refused = secure_client.post(
            f"/tickets/{cast.ticket.id}/close",
            json={"state": "resolved", "resolution": "not mine to close"},
            headers=cast.token(cast.other_agent),
        )
        assert refused.status_code == 403

        closed = secure_client.post(
            f"/tickets/{cast.ticket.id}/close",
            json={"state": "resolved", "resolution": "refunded by hand"},
            headers=cast.token(cast.agent),
        )
        assert closed.status_code == 200
        assert closed.json()["state"] == "resolved"


class TestDecidingAnEscalationThroughTheApi:
    def test_a_supervisor_approves_and_rejects(
        self, secure_client: TestClient, cast: Cast, store: Store
    ) -> None:
        approval_id = store.open_approval(
            cast.session_id, CANCEL, requested_by=cast.agent, ticket_id=cast.ticket.id
        )
        listed = secure_client.get("/approvals", headers=cast.token(cast.supervisor))
        assert listed.status_code == 200
        assert approval_id in [row["id"] for row in listed.json()]

        decided = secure_client.post(
            f"/approvals/{approval_id}/decision",
            json={"approve": True},
            headers=cast.token(cast.supervisor),
        )
        assert decided.status_code == 200
        assert decided.json()["state"] == "approved"

    def test_nobody_decides_their_own_escalation(
        self, secure_client: TestClient, cast: Cast, store: Store
    ) -> None:
        approval_id = store.open_approval(
            cast.session_id, CANCEL, requested_by=cast.supervisor, ticket_id=cast.ticket.id
        )
        response = secure_client.post(
            f"/approvals/{approval_id}/decision",
            json={"approve": True},
            headers=cast.token(cast.supervisor),
        )
        assert response.status_code == 403

    def test_an_expired_request_is_refused_with_409(
        self, secure_client: TestClient, cast: Cast, store: Store
    ) -> None:
        approval_id = store.open_approval(
            cast.session_id, CANCEL, requested_by=cast.agent, ttl_seconds=-1.0
        )
        response = secure_client.post(
            f"/approvals/{approval_id}/decision",
            json={"approve": True},
            headers=cast.token(cast.supervisor),
        )
        assert response.status_code == 409

    def test_an_agent_cannot_decide(
        self, secure_client: TestClient, cast: Cast, store: Store
    ) -> None:
        approval_id = store.open_approval(cast.session_id, CANCEL, requested_by=cast.agent)
        response = secure_client.post(
            f"/approvals/{approval_id}/decision",
            json={"approve": True},
            headers=cast.token(cast.agent),
        )
        assert response.status_code == 403

    def test_an_unknown_request_is_404(self, secure_client: TestClient, cast: Cast) -> None:
        response = secure_client.post(
            "/approvals/999999/decision",
            json={"approve": True},
            headers=cast.token(cast.supervisor),
        )
        assert response.status_code == 404


class TestTheTimingRoute:
    def test_it_reports_the_split(self, secure_client: TestClient, cast: Cast) -> None:
        response = secure_client.get(
            f"/session/{cast.session_id}/timing", headers=cast.token(cast.supervisor)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["waiting_for_human_seconds"] > 0
        assert body["system_seconds"] + body["waiting_for_human_seconds"] == pytest.approx(
            body["total_seconds"], abs=1e-6
        )

    def test_a_customer_cannot_read_it(self, secure_client: TestClient, cast: Cast) -> None:
        response = secure_client.get(
            f"/session/{cast.session_id}/timing", headers=cast.token(cast.customer)
        )
        assert response.status_code == 403
