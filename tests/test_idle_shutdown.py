"""The idle verdict, and the watcher that acts on it.

Two halves. The route decides what "idle" means for this application; the watcher decides
what to do about it. Both are tested here because a wrong answer on either side has the
same two consequences: a machine that never stops, or one that stops on somebody.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

_spec = importlib.util.spec_from_file_location(
    "idle_shutdown", REPO / "deploy" / "instance" / "idle_shutdown.py"
)
assert _spec and _spec.loader
idle_shutdown = importlib.util.module_from_spec(_spec)
# Registered before it is executed: its dataclasses resolve their own annotations through
# sys.modules, and a module that is not there raises during class creation.
sys.modules["idle_shutdown"] = idle_shutdown
_spec.loader.exec_module(idle_shutdown)

Answer = idle_shutdown.Answer
decide = idle_shutdown.decide

IDLE_REQUIRED = 15 * 60.0
CAP = 240 * 60.0


def at(answer: Answer, uptime: float = 60.0) -> idle_shutdown.Decision:
    return decide(
        answer,
        uptime,
        idle_seconds_required=IDLE_REQUIRED,
        max_uptime_seconds=CAP,
    )


class TestTheWatcherDecision:
    def test_idle_long_enough_stops_the_machine(self) -> None:
        assert at(Answer(reachable=True, idle_seconds=IDLE_REQUIRED)).shutdown

    def test_one_second_short_of_the_window_does_not(self) -> None:
        decision = at(Answer(reachable=True, idle_seconds=IDLE_REQUIRED - 1))
        assert not decision.shutdown
        assert "more minutes" in decision.reason

    def test_anything_holding_keeps_it_up_however_long_it_has_been_quiet(self) -> None:
        """Time alone must never be enough.

        An unanswered confirmation produces no requests, so a clock would call it idle
        after the window and stop the machine on a half-finished write.
        """
        answer = Answer(
            reachable=True,
            idle_seconds=10 * IDLE_REQUIRED,
            holding=("1 write(s) waiting for confirmation",),
        )
        decision = at(answer)
        assert not decision.shutdown
        assert "confirmation" in decision.reason

    def test_every_holding_reason_reaches_the_log(self) -> None:
        answer = Answer(
            reachable=True,
            idle_seconds=0.0,
            holding=("2 approval(s) waiting for a human", "3 open ticket(s)"),
        )
        reason = at(answer).reason
        assert "approval" in reason
        assert "ticket" in reason

    def test_an_unreachable_application_is_not_idle(self) -> None:
        decision = at(Answer.unreachable("ConnectionRefusedError: [Errno 111]"))
        assert not decision.shutdown
        assert "did not answer" in decision.reason

    def test_the_cap_fires_when_the_application_never_answers(self) -> None:
        decision = at(Answer.unreachable("timed out"), uptime=CAP)
        assert decision.shutdown
        assert "past the cap" in decision.reason

    def test_the_cap_beats_a_session_that_is_stuck_open(self) -> None:
        """Otherwise one wedged session bills a machine until somebody notices."""
        answer = Answer(reachable=True, idle_seconds=0.0, holding=("1 open ticket(s)",))
        assert at(answer, uptime=CAP + 1).shutdown

    def test_the_cap_does_not_fire_early(self) -> None:
        answer = Answer(reachable=True, idle_seconds=0.0, holding=("1 open ticket(s)",))
        assert not at(answer, uptime=CAP - 1).shutdown


class TestReadingTheAnswer:
    def test_a_body_without_the_fields_is_treated_as_busy(self) -> None:
        """A changed or truncated body must not read as 'nobody is here'."""
        answer = Answer(reachable=True)
        assert not at(answer).shutdown

    def test_uptime_that_cannot_be_read_reports_zero(self, monkeypatch) -> None:
        """Zero disables the cap. A bad read must not look like a machine that is overdue."""
        with tempfile.TemporaryDirectory() as directory:
            monkeypatch.setattr(idle_shutdown, "UPTIME_PATH", str(Path(directory) / "absent"))
            assert idle_shutdown.uptime_seconds() == 0.0
        assert not at(Answer.unreachable("no answer"), uptime=0.0).shutdown

    def test_uptime_is_read_from_the_first_field(self, monkeypatch) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uptime"
            path.write_text("864000.42 1234.00" + chr(10), encoding="ascii")
            monkeypatch.setattr(idle_shutdown, "UPTIME_PATH", str(path))
            assert idle_shutdown.uptime_seconds() == pytest.approx(864000.42)


class TestTheIdleRoute:
    """The route, against a real app with no database.

    M0 mode has no store, so the three business conditions are structurally zero and the
    route must still answer rather than fail on a missing store.
    """

    @pytest.fixture
    def client(self) -> TestClient:
        from retail_support_agent.api import create_app
        from retail_support_agent.config import Settings

        settings = Settings(DASHSCOPE_API_KEY="not-a-real-key-for-tests")  # type: ignore[call-arg]
        return TestClient(create_app(settings=settings, store=None))

    def test_a_fresh_service_reports_itself_idle(self, client: TestClient) -> None:
        body = client.get("/idle").json()
        assert body["holding"] == []
        assert body["idle_seconds"] >= 0.0
        assert body["persistent"] is False

    def test_polling_the_idle_route_does_not_reset_the_clock(self, client: TestClient) -> None:
        """The watcher polls once a minute. If that counted, nothing would ever stop.

        The clock is pushed back an hour first. Without that, every reading is near zero
        and the test passes whether or not the poll resets anything.
        """
        client.app.state.last_activity = time.monotonic() - 3600.0
        for _ in range(3):
            client.get("/idle")
            client.get("/health")
        assert client.get("/idle").json()["seconds_since_request"] > 3500.0

    def test_an_hour_of_quiet_counts_as_an_hour_of_idleness(self, client: TestClient) -> None:
        client.app.state.last_activity = time.monotonic() - 3600.0
        assert client.get("/idle").json()["idle_seconds"] > 3500.0

    def test_a_real_request_does_reset_the_clock(self, client: TestClient) -> None:
        client.app.state.last_activity = time.monotonic() - 3600.0
        client.get("/tickets")
        assert client.get("/idle").json()["seconds_since_request"] < 1.0

    def test_a_request_to_a_route_that_does_not_exist_still_counts(
        self, client: TestClient
    ) -> None:
        """Somebody is there. A 404 says they asked for the wrong thing, not that they left."""
        client.app.state.last_activity = time.monotonic() - 3600.0
        client.get("/no-such-route")
        assert client.get("/idle").json()["seconds_since_request"] < 1.0

    def test_the_session_count_is_reported_but_holds_nothing(self, client: TestClient) -> None:
        """SessionRegistry never releases a session, so this count cannot gate a shutdown.

        See M6.3 section 1.1.1. If this ever becomes a holding reason again, the machine
        stops being able to stop.
        """
        body = client.get("/idle").json()
        assert "active_sessions" in body
        assert body["holding"] == []


TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")


class TestTheIdleRouteAgainstARealDatabase:
    """The three business conditions, read out of PostgreSQL.

    The route class above runs with no store, so every count there is structurally zero.
    These are the cases that decide whether a machine stops on somebody, and they need
    rows that exist.
    """

    @pytest.fixture(scope="class")
    def store(self):
        from retail_support_agent.db import Base, make_engine, make_session_factory
        from retail_support_agent.store import Store

        if not TEST_URL:
            if os.getenv("CI"):
                raise RuntimeError(
                    "CI must provide RSA_TEST_DATABASE_URL for the job running these tests. "
                    "Skipping them means the idle verdict is never checked against real rows."
                )
            pytest.skip(
                "needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (docker compose up -d db)"
            )
        engine = make_engine(TEST_URL)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        yield Store(make_session_factory(engine))
        Base.metadata.drop_all(engine)
        engine.dispose()

    @pytest.fixture
    def client(self, store) -> TestClient:
        from retail_support_agent.api import create_app
        from retail_support_agent.config import Settings

        settings = Settings(DASHSCOPE_API_KEY="not-a-real-key-for-tests")  # type: ignore[call-arg]
        return TestClient(create_app(settings=settings, store=store))

    def test_an_open_ticket_holds_the_machine_open(self, store, client: TestClient) -> None:
        session_id = f"s-{uuid.uuid4().hex[:12]}"
        store.ensure_session(session_id, "ava_moore_2033", None)
        ticket = store.open_ticket(session_id, "ava_moore_2033", "held open", "test")

        client.app.state.last_activity = time.monotonic() - 3600.0
        body = client.get("/idle").json()
        assert body["open_tickets"] >= 1
        assert body["idle_seconds"] == 0.0
        assert any("ticket" in reason for reason in body["holding"])

        store.close_ticket(ticket.id, "resolved", "done")
        client.app.state.last_activity = time.monotonic() - 3600.0
        after = client.get("/idle").json()
        assert after["open_tickets"] == 0
        assert after["holding"] == []
        assert after["idle_seconds"] > 3500.0

    def test_a_write_waiting_for_confirmation_holds_the_machine_open(
        self, store, client: TestClient
    ) -> None:
        from retail_support_agent.guardrails import ProposedCall

        session_id = f"s-{uuid.uuid4().hex[:12]}"
        store.ensure_session(session_id, "ava_moore_2033", None)
        store.open_confirmation(
            session_id,
            ProposedCall("cancel_pending_order", {"order_id": "#W001", "reason": "test"}),
        )

        client.app.state.last_activity = time.monotonic() - 3600.0
        body = client.get("/idle").json()
        assert body["pending_confirmations"] >= 1
        assert body["idle_seconds"] == 0.0
        assert any("confirmation" in reason for reason in body["holding"])

    def test_time_alone_never_makes_a_held_machine_idle(self, store, client: TestClient) -> None:
        """The case a clock gets wrong.

        Nobody is sending requests, because they are waiting for an answer that has not
        come. A watcher measuring silence would stop the machine on a half-finished write.
        """
        from retail_support_agent.guardrails import ProposedCall

        session_id = f"s-{uuid.uuid4().hex[:12]}"
        store.ensure_session(session_id, "ava_moore_2033", None)
        store.open_confirmation(
            session_id, ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "t"})
        )
        client.app.state.last_activity = time.monotonic() - 24 * 3600.0
        answer = idle_shutdown.Answer(
            reachable=True,
            idle_seconds=client.get("/idle").json()["idle_seconds"],
            holding=tuple(client.get("/idle").json()["holding"]),
        )
        assert not at(answer).shutdown
