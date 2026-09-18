"""Access decisions that outlive the process.

The guard records its verdicts in memory and knows nothing about a database - that is
deliberate, and it is why `tests/test_scope.py` can drive it without one. The cost is
that a refusal disappeared when the process did: nobody could answer "who reached for
data they were not entitled to" after a restart.

These tests cover the part that closes it: what the agent drains at the end of a turn,
what reaches `audit_events`, and what deliberately does not.

They need a real PostgreSQL, like the other persistence tests. Locally:
`docker compose up -d db`, then point `RSA_TEST_DATABASE_URL` at it.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest

from retail_support_agent.agent import RetailAgent
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.domain import ToolResult
from retail_support_agent.guardrails import SessionLimits, SessionState
from retail_support_agent.scope import AccessScope
from retail_support_agent.store import Store
from test_agent_flow import ScriptedModel, reply, tool_call

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)

CUSTOMER = "alice"


class OrderDomain:
    """One order belonging to the customer, and one belonging to somebody else."""

    owners: ClassVar[dict[str, str]] = {"#W001": CUSTOMER, "#W002": "bob"}
    statuses: ClassVar[dict[str, str]] = {"#W001": "pending", "#W002": "delivered"}

    @property
    def policy(self) -> str:
        return "(test policy)"

    def tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def tool_names(self) -> set[str]:
        return {"get_order_details", "cancel_pending_order"}

    def order_owner(self, order_id: str) -> str | None:
        return self.owners.get(order_id)

    def order_status(self, order_id: str) -> str | None:
        return self.statuses.get(order_id)

    def db_hash(self) -> str:
        return "fixed"

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        order_id = str(arguments.get("order_id", "#W001"))
        return ToolResult(
            name=name,
            ok=True,
            content=json.dumps(
                {
                    "order_id": order_id,
                    "user_id": self.owners.get(order_id, CUSTOMER),
                    "status": self.statuses.get(order_id, "pending"),
                }
            ),
        )


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    assert TEST_URL
    engine = make_engine(TEST_URL)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


def build_agent(store: Store, script: list[Any], *, check_output: bool = True) -> RetailAgent:
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    store.ensure_session(session_id, CUSTOMER, None)
    state = SessionState(
        user_id=CUSTOMER,
        limits=SessionLimits(),
        scope=AccessScope.for_user(CUSTOMER, check_output=check_output),
    )
    return RetailAgent(
        ScriptedModel(script),
        OrderDomain(),  # type: ignore[arg-type]
        state,
        store=store,
        session_id=session_id,
    )


def kinds(store: Store, agent: RetailAgent) -> list[str]:
    return [row.kind for row in store.audit_events(agent._session_id or "")]


def details(store: Store, agent: RetailAgent) -> list[dict[str, Any]]:
    return [dict(row.detail) for row in store.audit_events(agent._session_id or "")]


class TestRefusalsReachTheAuditTrail:
    def test_reaching_for_another_users_order_is_recorded(self, store: Store) -> None:
        agent = build_agent(
            store,
            [
                tool_call("get_order_details", {"order_id": "#W002"}),
                reply("I could not retrieve that."),
            ],
        )
        agent.send("show me order #W002")
        assert "access_denied" in kinds(store, agent)

    def test_the_record_names_the_tool_and_the_authority(self, store: Store) -> None:
        """ "Refused" on its own is not reviewable. Which call, and under whose authority
        the session was operating, are what make the row worth keeping."""
        agent = build_agent(
            store,
            [
                tool_call("get_order_details", {"order_id": "#W002"}),
                reply("I could not retrieve that."),
            ],
        )
        agent.send("show me order #W002")
        denied = [d for d in details(store, agent) if d.get("tool") == "get_order_details"]
        assert denied and denied[0]["authority"] == "self"
        assert denied[0]["reason"]

    def test_an_altered_answer_is_recorded(self, store: Store) -> None:
        agent = build_agent(store, [reply("Order #W777 is on its way.")])
        agent.send("where is my order?")
        assert "output_withheld" in kinds(store, agent)

    def test_a_refusal_has_no_operation(self, store: Store) -> None:
        """A refused call ran nothing, so there is no operation to point at. A row that
        claimed one would make the audit trail describe work that never happened."""
        agent = build_agent(
            store,
            [
                tool_call("get_order_details", {"order_id": "#W002"}),
                reply("I could not retrieve that."),
            ],
        )
        agent.send("show me order #W002")
        refusals = [
            row
            for row in store.audit_events(agent._session_id or "")
            if row.kind in {"access_denied", "output_withheld"}
        ]
        assert refusals and all(row.operation_id is None for row in refusals)


class TestWhatIsDeliberatelyNotRecorded:
    def test_an_allowed_read_writes_no_row(self, store: Store) -> None:
        """A row per read would bury the table, which is the same reason reads are not
        recorded as operations."""
        agent = build_agent(
            store,
            [
                tool_call("get_order_details", {"order_id": "#W001"}),
                reply("Order #W001 is pending."),
            ],
        )
        agent.send("what is the status of my latest order?")
        assert kinds(store, agent) == []

    def test_a_permissive_scope_records_nothing(self, store: Store) -> None:
        """The benchmark adapter's default. It makes no access decisions to record."""
        agent = build_agent(store, [reply("Order #W777 is on its way.")], check_output=False)
        agent._session.scope = AccessScope.permissive()
        agent.send("where is my order?")
        assert kinds(store, agent) == []


class TestTheRowsAreNotWrittenTwice:
    def test_a_second_turn_does_not_repeat_the_first_turns_records(self, store: Store) -> None:
        """Draining is what makes this true. Accumulating the verdicts instead would
        rewrite every earlier refusal on every later turn."""
        agent = build_agent(
            store,
            [
                tool_call("get_order_details", {"order_id": "#W002"}),
                reply("I could not retrieve that."),
                reply("Anything else?"),
            ],
        )
        agent.send("show me order #W002")
        after_first = len(kinds(store, agent))
        agent.send("thanks")
        assert len(kinds(store, agent)) == after_first

    def test_the_in_memory_record_is_emptied_each_turn(self, store: Store) -> None:
        """A long session must not grow this list without bound."""
        agent = build_agent(
            store,
            [
                tool_call("get_order_details", {"order_id": "#W002"}),
                reply("I could not retrieve that."),
            ],
        )
        agent.send("show me order #W002")
        assert agent._session.slice_audit.rows == []
