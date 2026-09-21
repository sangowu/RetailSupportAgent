"""One session runs one turn at a time, and different sessions do not wait for each other.

`RetailAgent.send` has always documented that calls for one session must be serialized.
Across processes `Store.claim_session` enforces it, and `test_recovery.TestOneOwnerPerSession`
asserts that. Inside one process nothing did: the claim renews rather than refuses for the
owner that already holds it, which is correct for a process resuming its own session and
grants every concurrent request within it. `/chat` is a sync route, so FastAPI runs it in
the threadpool and those requests are genuinely parallel.

What that cost, measured before it was fixed - two concurrent messages to one session:

    peak concurrent model calls: 2
    result: ('first message',  200, 'ok')
    result: ('second message', 200, 'ok')
    journaled: [('user', 'first message'), ('assistant', 'ok')]
    progress: steps=1

Both callers were told their message had been handled. One of them was not in the record:
`_journal` reads the next sequence number and then inserts, so two turns computed the same
one and the loser's row was swallowed as a duplicate - a swallow written for a resent turn,
eating a genuinely different message. And two turns advanced the step counter once, which
makes concurrency a way around every stop condition, the same hole restarting used to be.

These need a real PostgreSQL: what is being checked is what reached the database.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from retail_support_agent import api as api_module
from retail_support_agent.api import create_app
from retail_support_agent.config import Settings
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.store import Store

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

# Long enough that two turns starting together must overlap, short enough that a module
# of these tests stays quick.
TURN_SECONDS = 0.4


@pytest.fixture(autouse=True)
def _a_key_that_is_never_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings()` requires a model key, and `conftest` clears the real one.

    Every test here replaces the model outright, so the value is never sent anywhere.
    Without it these passed on a machine with a populated `.env` and failed in CI, which
    is the wrong way round for a credential.
    """
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")


@pytest.fixture
def store() -> Iterator[Store]:
    if not TEST_URL:
        if os.getenv("CI"):
            raise RuntimeError(
                "CI must provide RSA_TEST_DATABASE_URL for the job running these tests. "
                "Skipping them means the concurrency threshold never ran while CI still "
                "reports green."
            )
        pytest.skip("needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (docker compose up -d db)")
    engine = make_engine(TEST_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


class Meter:
    """A model that is slow on purpose and counts how many calls overlap.

    The peak is the measurement. Asserting on reply contents would not distinguish two
    turns that ran in sequence from two that ran on top of each other.
    """

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0
        self.calls = 0
        self._guard = threading.Lock()

    def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        with self._guard:
            self.inflight += 1
            self.calls += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(TURN_SECONDS)
        with self._guard:
            self.inflight -= 1
        return _completion("ok")


def _completion(text: str) -> Any:
    """The shape `make_chat_fn` returns, with no tool calls: one answer, one step."""

    class Message:
        content: ClassVar[str] = text
        tool_calls: ClassVar[list[Any]] = []

    class Choice:
        message: ClassVar[Message] = Message()

    class Completion:
        choices: ClassVar[list[Choice]] = [Choice()]

    return Completion()


@pytest.fixture
def meter(monkeypatch: pytest.MonkeyPatch) -> Meter:
    """Replaces the model for the whole app. No network and no billing."""
    instrument = Meter()
    monkeypatch.setattr(api_module, "make_chat_fn", lambda settings, config: instrument)
    return instrument


def new_session() -> str:
    return f"c-{uuid.uuid4().hex[:10]}"


def post_all(client: TestClient, calls: list[tuple[str, str]]) -> list[Any]:
    """Fire every (session_id, message) at once and return the responses in order."""
    answers: list[Any] = [None] * len(calls)

    def one(index: int, session_id: str, message: str) -> None:
        answers[index] = client.post("/chat", json={"session_id": session_id, "message": message})

    threads = [
        threading.Thread(target=one, args=(i, session_id, message))
        for i, (session_id, message) in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return answers


class TestOneTurnPerSession:
    def test_a_second_message_is_refused_while_the_first_is_running(
        self, store: Store, meter: Meter
    ) -> None:
        session_id = new_session()
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            answers = post_all(client, [(session_id, "first"), (session_id, "second")])

        codes = sorted(answer.status_code for answer in answers)
        assert codes == [200, 409]
        assert meter.peak == 1, "two turns ran on one session at the same time"

    def test_the_refusal_says_the_session_is_busy(self, store: Store, meter: Meter) -> None:
        """409, and worded for both reasons a session can be busy. A caller cannot act on
        the difference between another process and another request."""
        session_id = new_session()
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            answers = post_all(client, [(session_id, "first"), (session_id, "second")])

        refused = next(answer for answer in answers if answer.status_code == 409)
        assert "already handling a message" in refused.json()["detail"]

    def test_no_message_is_silently_dropped(self, store: Store, meter: Meter) -> None:
        """The defect that made this more than a tidiness question.

        Both callers used to get 200 and only one message reached `messages`. Whatever is
        refused must be refused out loud; whatever is accepted must be in the record.
        """
        session_id = new_session()
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            answers = post_all(client, [(session_id, "first"), (session_id, "second")])

        accepted = [
            message
            for answer, message in zip(answers, ["first", "second"], strict=True)
            if answer.status_code == 200
        ]
        journaled = [row.content for row in store.messages(session_id) if row.role == "user"]
        assert journaled == accepted

    def test_the_counters_advance_once_per_accepted_turn(self, store: Store, meter: Meter) -> None:
        """Two turns advancing the counter once made concurrency a way around every stop
        condition - the hole `test_a_restart_cannot_reset_a_reached_limit` closes for
        restarts."""
        session_id = new_session()
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            post_all(client, [(session_id, "first"), (session_id, "second")])

        progress = store.progress(session_id)
        assert progress is not None
        assert progress.steps == meter.calls

    def test_the_turn_is_released_when_it_ends(self, store: Store, meter: Meter) -> None:
        """A lock held past its turn would lock the session out for the life of the
        process, which is worse than the defect it prevents."""
        session_id = new_session()
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            first = client.post("/chat", json={"session_id": session_id, "message": "one"})
            second = client.post("/chat", json={"session_id": session_id, "message": "two"})

        assert first.status_code == 200
        assert second.status_code == 200

    def test_a_failed_turn_still_releases_it(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure path is where a lock gets stranded. A 502 must not cost the
        session every later message."""
        session_id = new_session()

        def explode(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
            raise RuntimeError("the model is down")

        monkeypatch.setattr(api_module, "make_chat_fn", lambda settings, config: explode)
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            failed = client.post("/chat", json={"session_id": session_id, "message": "one"})
            after = client.post("/chat", json={"session_id": session_id, "message": "two"})

        assert failed.status_code == 502
        assert after.status_code == 502, "a second attempt must reach the model, not a lock"


class TestSessionsDoNotWaitForEachOther:
    def test_two_sessions_run_at_the_same_time(self, store: Store, meter: Meter) -> None:
        """The lock is per session. One that serialized the whole process would turn a
        correctness fix into a throughput ceiling, and nothing in the design asks for
        one session's message to wait behind another's.
        """
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            answers = post_all(client, [(new_session(), "a"), (new_session(), "b")])

        assert [answer.status_code for answer in answers] == [200, 200]
        assert meter.peak == 2


class TestTheStreamingRouteHoldsItsTurnToo:
    def test_a_message_is_refused_while_a_stream_is_running(
        self, store: Store, meter: Meter
    ) -> None:
        """The turn ends after the last event, not when the route returns. `/chat/stream`
        resolves the agent and then hands back a generator; releasing at the return would
        leave the turn running with nothing holding it.
        """
        session_id = new_session()
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        refused: list[int] = []

        with TestClient(app) as client:

            def interrupt() -> None:
                time.sleep(TURN_SECONDS / 2)
                refused.append(
                    client.post(
                        "/chat", json={"session_id": session_id, "message": "second"}
                    ).status_code
                )

            thread = threading.Thread(target=interrupt)
            thread.start()
            with client.stream(
                "POST", "/chat/stream", json={"session_id": session_id, "message": "first"}
            ) as response:
                assert response.status_code == 200
                lines = [line for line in response.iter_lines() if line.startswith("data:")]
            thread.join()

        assert lines, "the stream produced no events"
        assert refused == [409]

    def test_the_stream_releases_its_turn_at_the_end(self, store: Store, meter: Meter) -> None:
        session_id = new_session()
        app = create_app(settings=Settings(), store=store)  # type: ignore[call-arg]
        with TestClient(app) as client:
            with client.stream(
                "POST", "/chat/stream", json={"session_id": session_id, "message": "first"}
            ) as response:
                list(response.iter_lines())
            after = client.post("/chat", json={"session_id": session_id, "message": "second"})

        assert after.status_code == 200
