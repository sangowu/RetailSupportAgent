"""Watching a turn happen, and proving it is the same turn (M7.1).

Two routes now reach one turn: `send`, which the benchmark uses and which this step
deliberately does not touch, and `stream`, which drives the same nodes one at a time so a
page can show the work. Two routes into one behaviour is a thing that drifts, so most of
what is here is the comparison rather than the events.

No network and no billing: a scripted model, as in test_agent_flow.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from retail_support_agent.agent import RetailAgent, TurnEvent, TurnResult
from retail_support_agent.api import create_app
from retail_support_agent.config import Settings
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.guardrails import SessionLimits, SessionState
from retail_support_agent.store import Store
from test_agent_flow import FakeDomain, ScriptedModel, reply, tool_call

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")


def build(
    script: list[Any], store: Store | None = None, session_id: str | None = None
) -> RetailAgent:
    domain = FakeDomain()
    state = SessionState(user_id="alice", limits=SessionLimits())
    return RetailAgent(ScriptedModel(script), domain, state, store, session_id)  # type: ignore[arg-type]


def kinds(agent: RetailAgent, message: str) -> list[str]:
    return [event.kind for event in agent.stream(message)]


def drain(agent: RetailAgent, message: str) -> list[TurnEvent]:
    return list(agent.stream(message))


class TestWhatTheEventsSay:
    def test_something_is_emitted_before_the_model_is_called(self) -> None:
        """The first model call is the longest silence in a turn. A first event that
        waited for it would leave the panel blank for exactly as long as before."""
        agent = build([reply("Hello.")])
        events = agent.stream("hi")
        assert next(events).kind == "thinking"

    def test_a_plain_answer_is_two_events(self) -> None:
        assert kinds(build([reply("Hello.")]), "hi") == ["thinking", "reply"]

    def test_a_tool_appears_as_its_own_step(self) -> None:
        agent = build(
            [tool_call("get_order_details", {"order_id": "#W001"}), reply("It is pending.")]
        )
        events = drain(agent, "look up #W001")
        assert [e.kind for e in events] == ["thinking", "tool", "thinking", "reply"]
        assert events[1].tool == "get_order_details"

    def test_a_held_write_is_an_event_not_a_sentence(self) -> None:
        """The claim this project exists to demonstrate. A viewer has to see the guard
        hold the write, not read afterwards that it did."""
        agent = build([tool_call("cancel_pending_order", {"order_id": "#W001"})])
        events = drain(agent, "cancel #W001")
        held = [e for e in events if e.kind == "held"]
        assert len(held) == 1
        assert held[0].tool == "cancel_pending_order"
        assert "#W001" in held[0].detail

    def test_a_refusal_is_its_own_event_and_says_why(self) -> None:
        agent = build([tool_call("cancel_pending_order", {"order_id": "#W002"})])
        events = drain(agent, "cancel bob's order")
        refused = [e for e in events if e.kind == "refused"]
        assert refused, [e.kind for e in events]
        assert refused[0].detail

    def test_the_last_event_carries_the_whole_result(self) -> None:
        """A caller that keeps only the last event is a caller of `send`."""
        events = drain(build([reply("Hello.")]), "hi")
        assert events[-1].kind == "reply"
        assert events[-1].result is not None
        assert events[-1].result.reply == "Hello."

    def test_no_other_event_carries_a_result(self) -> None:
        events = drain(
            build([tool_call("get_order_details", {"order_id": "#W001"}), reply("Pending.")]),
            "look up #W001",
        )
        assert [e.result is not None for e in events] == [False, False, False, True]

    def test_a_stopped_session_still_answers(self) -> None:
        """The stop is checked before the model is called, so the turn ends in the first
        node and the stream must still produce a result rather than nothing."""
        domain = FakeDomain()
        state = SessionState(user_id="alice", limits=SessionLimits(max_steps=0))
        agent = RetailAgent(ScriptedModel([reply("unused")]), domain, state)  # type: ignore[arg-type]
        events = drain(agent, "hello")
        assert events[-1].result is not None
        assert events[-1].result.status == "stopped"


class TestTheTwoRoutesAgree:
    """The failure this guards against is silent: a streaming route that answers
    correctly while recording one row fewer looks right in the demo and is wrong in the
    audit trail, which is the artefact this project is built to produce."""

    SCRIPT: ClassVar[list[Any]] = [
        tool_call("get_order_details", {"order_id": "#W001"}),
        reply("Order #W001 is pending."),
    ]

    def test_the_same_input_gives_the_same_result(self) -> None:
        sent = build(list(self.SCRIPT)).send("look up #W001")
        streamed = drain(build(list(self.SCRIPT)), "look up #W001")[-1].result
        assert streamed is not None
        assert (sent.reply, sent.status, sent.tool_calls) == (
            streamed.reply,
            streamed.status,
            streamed.tool_calls,
        )

    def test_the_same_tools_run(self) -> None:
        one = build(list(self.SCRIPT))
        two = build(list(self.SCRIPT))
        one.send("look up #W001")
        drain(two, "look up #W001")
        assert one._domain.executed == two._domain.executed  # type: ignore[attr-defined]

    def test_a_held_write_is_held_both_ways(self) -> None:
        script: list[Any] = [tool_call("cancel_pending_order", {"order_id": "#W001"})]
        sent = build(list(script)).send("cancel #W001")
        streamed = drain(build(list(script)), "cancel #W001")[-1].result
        assert streamed is not None
        assert sent.status == streamed.status == "needs_confirmation"
        assert sent.pending_confirmation == streamed.pending_confirmation


@pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)
class TestTheTwoRoutesWriteTheSameRows:
    """Counts are not the point on their own; the point is that neither route is a
    second, quieter way of doing business."""

    @staticmethod
    @pytest.fixture
    def store() -> Store:
        assert TEST_URL
        engine = make_engine(TEST_URL)
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            for table in ("audit_events", "operations", "confirmations", "messages", "sessions"):
                connection.execute(text(f"delete from {table}"))
        return Store(make_session_factory(engine))

    def rows(self, store: Store, session_id: str) -> tuple[int, int, int]:
        return (
            len(store.messages(session_id)),
            len(store.operations(session_id)),
            len(store.audit_events(session_id)),
        )

    def test_a_read_turn_records_the_same_rows(self, store: Store) -> None:
        script: list[Any] = [
            tool_call("get_order_details", {"order_id": "#W001"}),
            reply("Order #W001 is pending."),
        ]
        build(list(script), store, "sent").send("look up #W001")
        drain(build(list(script), store, "streamed"), "look up #W001")
        assert self.rows(store, "sent") == self.rows(store, "streamed")

    def test_a_confirmed_write_records_the_same_rows(self, store: Store) -> None:
        script: list[Any] = [
            tool_call("cancel_pending_order", {"order_id": "#W001"}),
            tool_call("cancel_pending_order", {"order_id": "#W001"}, call_id="c2"),
            reply("Cancelled."),
        ]
        sent = build(list(script), store, "sent2")
        sent.send("cancel #W001")
        sent.send("confirm")

        streamed = build(list(script), store, "streamed2")
        drain(streamed, "cancel #W001")
        drain(streamed, "confirm")

        assert self.rows(store, "sent2") == self.rows(store, "streamed2")
        assert self.rows(store, "sent2")[1] == 1, "the write should have been recorded once"


@pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)
class TestAnAnswerNamesTheCase:
    """M7.3. The number used to exist only inside the reply's prose, so a page that wanted
    to link to the case had to dig it out of a sentence."""

    @staticmethod
    @pytest.fixture
    def store() -> Store:
        assert TEST_URL
        engine = make_engine(TEST_URL)
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            for table in ("tickets", "messages", "sessions"):
                connection.execute(text(f"delete from {table}"))
        return Store(make_session_factory(engine))

    @pytest.mark.parametrize("status", ("handed_off", "waiting_for_human"))
    def test_a_session_behind_a_case_reports_its_id(
        self, store: Store, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        session_id = f"cased-{status}"
        store.ensure_session(session_id, "ava_moore_2033")
        ticket = store.open_ticket(session_id, "ava_moore_2033", "a refund question", None)

        def fake(self: object, message: str) -> Any:
            yield TurnEvent(kind="reply", result=TurnResult(reply="x", status=status))  # type: ignore[arg-type]

        monkeypatch.setattr(RetailAgent, "stream", fake)
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with (
            TestClient(app) as client,
            client.stream(
                "POST", "/chat/stream", json={"session_id": session_id, "message": "hello"}
            ) as response,
        ):
            events = [
                json.loads(line[len("data:") :])
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert events[-1]["ticket_id"] == ticket.id

    def test_a_session_with_no_case_reports_none(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")

        def fake(self: object, message: str) -> Any:
            yield TurnEvent(kind="reply", result=TurnResult(reply="x", status="handed_off"))

        monkeypatch.setattr(RetailAgent, "stream", fake)
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with (
            TestClient(app) as client,
            client.stream(
                "POST", "/chat/stream", json={"session_id": "no-case", "message": "hello"}
            ) as response,
        ):
            events = [
                json.loads(line[len("data:") :])
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert events[-1]["ticket_id"] is None


class TestBothRoutesKeepTheSameClock:
    """The session's own bookkeeping, not just its answer.

    `send` and `stream` are two hand-written copies of one turn, held together by the
    comparisons above - which compare the reply and the database. A supervisor's
    deliberation stopped being charged to the session in `send` and went on being charged
    in `stream`, and every one of those comparisons still passed, because the clock is
    neither the reply nor a row. The page streams, so the fix reached production dead.
    """

    @staticmethod
    def escalating(route: str) -> SessionState:
        class WriteDomain(FakeDomain):
            def tool_names(self) -> set[str]:
                return {"cancel_pending_order"}

            def order_owner(self, order_id: str) -> str | None:
                return "alice"

            def order_status(self, order_id: str) -> str | None:
                return "pending"

        class SlowModel(ScriptedModel):
            """Scripted, but not instantaneous.

            A scripted turn finishes inside one tick of the platform clock, so a correct
            clock and a clock that never started both read 0.0. The pause is what makes
            "the turn was charged" a claim the test can tell apart from its negation.
            """

            def __call__(self, *args: Any, **kwargs: Any) -> Any:
                time.sleep(0.05)
                return super().__call__(*args, **kwargs)

        state = SessionState(user_id="alice", limits=SessionLimits(), acting_for_other=True)
        agent = RetailAgent(
            SlowModel([tool_call("cancel_pending_order", {"order_id": "#W001"})]),
            WriteDomain(),  # type: ignore[arg-type]
            state,
        )
        if route == "send":
            assert agent.send("cancel it").status == "needs_approval"
        else:
            events = list(agent.stream("cancel it"))
            assert events[-1].result is not None
            assert events[-1].result.status == "needs_approval"
        return state

    @pytest.mark.parametrize("route", ("send", "stream"))
    def test_a_finished_turn_leaves_no_clock_running(self, route: str) -> None:
        """Including one that ended waiting on a supervisor. A turn that returns without
        closing its own measurement charges the session for the wait that follows."""
        assert self.escalating(route).turn_started_at is None

    @pytest.mark.parametrize("route", ("send", "stream"))
    def test_the_turn_it_did_run_is_charged(self, route: str) -> None:
        """The other half: stopping the clock must not mean never starting it."""
        assert self.escalating(route).elapsed() >= 0.01

    @pytest.mark.parametrize("route", ("send", "stream"))
    def test_waiting_after_the_turn_costs_the_session_nothing(self, route: str) -> None:
        state = self.escalating(route)
        spent = state.elapsed()
        time.sleep(0.05)
        assert state.elapsed() == spent
