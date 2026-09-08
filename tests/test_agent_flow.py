"""End-to-end tests of the business loop with a fake model and fake domain: no
network, no billing.

The point is that the model's word does not count. Even when it proposes a write
directly or claims the user already confirmed, nothing runs until the user has
confirmed those specific arguments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from retail_support_agent.agent import RetailAgent, _is_affirmative
from retail_support_agent.domain import ToolResult
from retail_support_agent.guardrails import SessionLimits, SessionState

# ---- test doubles -------------------------------------------------------


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] = field(default_factory=list)


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]


def reply(text: str) -> FakeCompletion:
    return FakeCompletion([FakeChoice(FakeMessage(content=text))])


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> FakeCompletion:
    return FakeCompletion(
        [
            FakeChoice(
                FakeMessage(
                    content=None,
                    tool_calls=[FakeToolCall(call_id, FakeFunction(name, json.dumps(arguments)))],
                )
            )
        ]
    )


class ScriptedModel:
    """Return scripted responses in order and record the messages received, so tests
    can assert that tool results were sent back."""

    def __init__(self, script: list[FakeCompletion]) -> None:
        self.script = list(script)
        self.seen_messages: list[list[dict[str, Any]]] = []

    def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self.seen_messages.append([dict(m) for m in messages])
        return self.script.pop(0) if self.script else reply("(script exhausted)")


class FakeDomain:
    """Stands in for RetailDomain so tests do not depend on upstream data. Same interface."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.owners = {"#W001": "alice", "#W002": "bob"}

    @property
    def policy(self) -> str:
        return "(test policy)"

    def tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def tool_names(self) -> set[str]:
        return {"get_order_details", "cancel_pending_order"}

    def order_owner(self, order_id: str) -> str | None:
        return self.owners.get(order_id)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.executed.append((name, arguments))
        return ToolResult(name=name, ok=True, content=json.dumps({"status": "done"}))


def build(script: list[FakeCompletion], **limits: Any) -> tuple[RetailAgent, FakeDomain]:
    domain = FakeDomain()
    state = SessionState(user_id="alice", limits=SessionLimits(**limits))
    agent = RetailAgent(ScriptedModel(script), domain, state)  # type: ignore[arg-type]
    return agent, domain


# ---- tests --------------------------------------------------------------


class TestReadPath:
    def test_read_tool_executes_and_result_returns_to_model(self) -> None:
        agent, domain = build(
            [
                tool_call("get_order_details", {"order_id": "#W001"}),
                reply("Order #W001 is pending."),
            ]
        )
        result = agent.send("look up #W001")
        assert result.status == "answered"
        assert domain.executed == [("get_order_details", {"order_id": "#W001"})]
        assert "pending" in result.reply


class TestWriteRequiresConfirmation:
    def test_write_is_held_until_user_confirms(self) -> None:
        agent, domain = build([tool_call("cancel_pending_order", {"order_id": "#W001"})])
        result = agent.send("cancel #W001")

        assert result.status == "needs_confirmation"
        assert domain.executed == [], "a write must not run before confirmation"
        assert result.pending_confirmation is not None
        assert "#W001" in result.pending_confirmation

    def test_write_executes_after_explicit_confirmation(self) -> None:
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W001"}),
                tool_call("cancel_pending_order", {"order_id": "#W001"}, call_id="c2"),
                reply("Order #W001 has been cancelled."),
            ]
        )
        agent.send("cancel #W001")
        result = agent.send("confirm")

        assert result.status == "answered"
        assert domain.executed == [("cancel_pending_order", {"order_id": "#W001"})]

    def test_declining_keeps_the_write_unexecuted(self) -> None:
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W001"}),
                reply("Understood, nothing was changed."),
            ]
        )
        agent.send("cancel #W001")
        agent.send("no, don't")
        assert domain.executed == []

    def test_confirmation_does_not_transfer_to_different_arguments(self) -> None:
        """The user confirmed #W001; switching to #W002 needs a new confirmation, and
        is blocked by the ownership check as well."""
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W001"}),
                tool_call("cancel_pending_order", {"order_id": "#W002"}, call_id="c2"),
                reply("Done as requested."),
            ]
        )
        agent.send("cancel #W001")
        agent.send("confirm")
        assert domain.executed == [], "a confirmation covers only the arguments confirmed"


class TestOwnershipInFlow:
    def test_other_users_order_is_refused_with_reason(self) -> None:
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W002"}),
                reply("That order does not belong to you."),
            ]
        )
        result = agent.send("cancel #W002")
        assert domain.executed == []
        assert result.status in {"denied", "answered"}


class TestMalformedToolCall:
    def test_invalid_json_arguments_do_not_crash(self) -> None:
        broken = FakeCompletion(
            [FakeChoice(FakeMessage(None, [FakeToolCall("c1", FakeFunction("x", "{not json"))]))]
        )
        agent, domain = build([broken, reply("I need more precise information.")])
        result = agent.send("anything")
        assert domain.executed == []
        assert result.status == "answered"


class TestStopConditions:
    def test_step_limit_ends_the_turn(self) -> None:
        agent, _ = build([reply("hi")], max_steps=0)
        result = agent.send("hello")
        assert result.status == "stopped"
        assert result.stop_reason is not None


class TestAffirmativeDetection:
    @pytest.mark.parametrize("text", ["confirm", "yes", "OK", "sure, go ahead"])
    def test_affirmative(self, text: str) -> None:
        assert _is_affirmative(text)

    @pytest.mark.parametrize("text", ["no thanks", "not yet", "no", "cancel that", "don't"])
    def test_negative(self, text: str) -> None:
        assert not _is_affirmative(text)


class TestReportedToolCalls:
    """The `tool_calls` returned to the caller must be the tools actually executed
    during this turn.

    A real run exposed the bug: a turn is usually think (request tool) -> act
    (execute) -> think (answer), and the final think has no tool calls. Building a
    fresh empty list there discarded everything act had executed, so read-only queries
    always reported "no tools called" while they had queried the database. The UI and
    the API both display that field, so it under-reported to the user.

    The existing read-path test asserted only `domain.executed`, i.e. that the domain
    really ran it, and nothing asserted what we report, which is why this went
    unnoticed.
    """

    def test_read_query_reports_the_tool_it_used(self) -> None:
        agent, domain = build(
            [
                tool_call("get_order_details", {"order_id": "#W001"}),
                reply("Order #W001 is pending."),
            ]
        )
        result = agent.send("look up #W001")
        assert result.status == "answered"
        assert result.tool_calls == ["get_order_details"], "executed but not reported"
        # What we report must match what the domain executed.
        assert result.tool_calls == [name for name, _ in domain.executed]

    def test_multiple_rounds_all_get_reported(self) -> None:
        agent, _ = build(
            [
                tool_call("get_order_details", {"order_id": "#W001"}, call_id="c1"),
                tool_call("get_order_details", {"order_id": "#W002"}, call_id="c2"),
                reply("Found both orders."),
            ]
        )
        result = agent.send("look up #W001 and #W002")
        assert result.tool_calls == ["get_order_details", "get_order_details"]

    def test_each_turn_reports_only_its_own_tools(self) -> None:
        """The accumulator resets per turn, or the second turn would include the first
        turn's tools."""
        agent, _ = build(
            [
                tool_call("get_order_details", {"order_id": "#W001"}),
                reply("Found it."),
                reply("Anything else?"),
            ]
        )
        first = agent.send("look up #W001")
        second = agent.send("thanks")
        assert first.tool_calls == ["get_order_details"]
        assert second.tool_calls == [], "the second turn called no tools"

    def test_confirmed_write_is_reported(self) -> None:
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W001"}),
                tool_call("cancel_pending_order", {"order_id": "#W001"}, call_id="c2"),
                reply("Cancelled."),
            ]
        )
        agent.send("cancel #W001")
        result = agent.send("yes, confirm")
        assert result.tool_calls == ["cancel_pending_order"]
        assert domain.executed == [("cancel_pending_order", {"order_id": "#W001"})]
