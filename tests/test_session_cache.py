"""The session registry is a bounded cache, not a table that only grows (M8).

Each entry holds its own copy of the retail domain, and nothing evicted them. Measured
on this machine, one session after the first costs about 11.7 MB, and the demo runs on a
2 GB instance that also hosts PostgreSQL:

    sessions     unbounded     capacity 8
          20        514 MB         428 MB
          40        749 MB         427 MB
          60        983 MB         466 MB
          80       1218 MB         489 MB

Every page load that sends a message is a new session - the page mints a fresh id on
sign-in and on "New session" - so a visitor could exhaust the instance by reloading,
holding no permission and breaking no rule. Capped, the same eighty sessions stop the
process growing: what is left is allocator fragmentation, not live sessions.

Evicting ends nothing. The session's state is in the database and `recover()` rebuilds it
on the next message, which is the M2 path every restart already exercises. These tests
assert that: a session dropped from memory still answers, and still knows what it knew.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from retail_support_agent import api as api_module
from retail_support_agent.api import SessionRegistry, create_app
from retail_support_agent.auth import Identity
from retail_support_agent.config import Settings
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.llm import ModelConfig
from retail_support_agent.store import Store

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _a_key_that_is_never_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Settings()` requires a model key and `conftest` clears the real one. The model is
    replaced in every test here, so nothing is ever sent."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")


@pytest.fixture
def store() -> Iterator[Store]:
    if not TEST_URL:
        if os.getenv("CI"):
            raise RuntimeError(
                "CI must provide RSA_TEST_DATABASE_URL for the job running these tests. "
                "Skipping them means the cache ceiling never ran while CI reports green."
            )
        pytest.skip("needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (docker compose up -d db)")
    engine = make_engine(TEST_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


def _completion(text: str) -> Any:
    class Message:
        content: ClassVar[str] = text
        tool_calls: ClassVar[list[Any]] = []

    class Choice:
        message: ClassVar[Message] = Message()

    class Completion:
        choices: ClassVar[list[Choice]] = [Choice()]

    return Completion()


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    """Replaces the model and records what each turn was asked, so a test can check what
    a rebuilt session still knows."""
    seen: list[list[dict[str, Any]]] = []

    def model(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        seen.append([dict(message) for message in messages])
        return _completion("ok")

    monkeypatch.setattr(api_module, "make_chat_fn", lambda settings, config: model)
    return seen


@pytest.fixture
def app_with(store: Store, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build an app whose registry holds at most `capacity` sessions.

    Through the environment, which is how the setting is supplied in production.
    Passing it as a keyword does not work and does not say so: `Settings` declares
    `extra="ignore"`, so `Settings(session_cache_size=3)` is accepted, discarded, and
    leaves the default in place - which is how the first run of these tests came to
    assert a capacity nothing had applied.
    """

    def build(capacity: int) -> Any:
        monkeypatch.setenv("RSA_SESSION_CACHE_SIZE", str(capacity))
        return create_app(settings=Settings(), store=store)  # type: ignore[call-arg]

    return build


def new_session() -> str:
    return f"s-{uuid.uuid4().hex[:10]}"


def say(client: TestClient, session_id: str, message: str) -> Any:
    return client.post("/chat", json={"session_id": session_id, "message": message})


class TestTheCacheHasACeiling:
    def test_more_sessions_than_the_ceiling_do_not_all_stay(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        with TestClient(app_with(3)) as client:
            for _ in range(10):
                say(client, new_session(), "hello")
            assert client.get("/health").json()["active_sessions"] == 3

    def test_the_ceiling_is_configurable(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        """A deployment with more memory should be able to raise it. What it must not be
        is unbounded, which is what it was."""
        with TestClient(app_with(6)) as client:
            for _ in range(10):
                say(client, new_session(), "hello")
            assert client.get("/health").json()["active_sessions"] == 6

    def test_the_least_recently_used_session_is_the_one_dropped(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        """Order by use, not by creation: a session someone is still talking to must not
        be evicted because it happens to be old."""
        with TestClient(app_with(2)) as client:
            first, second = new_session(), new_session()
            say(client, first, "one")
            say(client, second, "two")
            say(client, first, "one again")  # first is now the most recent
            say(client, new_session(), "three")  # evicts something

            # `second` was the least recently used, so `first` is still in memory. Asking
            # it again must not have to rebuild, which is what the message count shows.
            before = len(store.messages(second))
            say(client, second, "two again")
            assert len(store.messages(second)) == before + 2


class TestEvictingEndsNothing:
    def test_a_dropped_session_still_answers(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        with TestClient(app_with(1)) as client:
            session_id = new_session()
            assert say(client, session_id, "first").status_code == 200
            say(client, new_session(), "pushes the first one out")
            assert client.get("/health").json()["active_sessions"] == 1

            again = say(client, session_id, "second")
            assert again.status_code == 200
            assert again.json()["reply"] == "ok"

    def test_a_dropped_session_still_knows_what_it_knew(
        self, app_with: Any, store: Store, answers: list[list[dict[str, Any]]]
    ) -> None:
        """The conversation is what the caller would notice losing. `recover()` restores
        it from `messages`, so the rebuilt session's next turn carries the earlier one."""
        with TestClient(app_with(1)) as client:
            session_id = new_session()
            say(client, session_id, "my first message")
            say(client, new_session(), "pushes the first one out")
            say(client, session_id, "my second message")

        last_turn = answers[-1]
        said = [message.get("content", "") for message in last_turn]
        assert any("my first message" in text for text in said)

    def test_a_dropped_session_keeps_its_counters(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        """Otherwise eviction is a way around the stop conditions, the same hole a
        restart used to be."""
        with TestClient(app_with(1)) as client:
            session_id = new_session()
            say(client, session_id, "one")
            say(client, new_session(), "pushes the first one out")
            say(client, session_id, "two")

        progress = store.progress(session_id)
        assert progress is not None
        assert progress.steps == 2

    def test_the_whole_record_survives(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        with TestClient(app_with(1)) as client:
            session_id = new_session()
            say(client, session_id, "one")
            say(client, new_session(), "pushes the first one out")
            say(client, session_id, "two")

        said = [row.content for row in store.messages(session_id) if row.role == "user"]
        assert said == ["one", "two"]


class TestASessionInTheMiddleOfATurnIsNotDropped:
    """Against the registry, not over HTTP.

    Two attempts to state this end to end were confounded, and both looked like passes:

    - the first let the held turn wait five seconds while each pressure request built its
      own domain, which takes longer, so the assertion read a session that had already
      finished - and passed while eviction ignored running turns entirely;
    - the second held the turn long enough to outlast `CLAIM_TTL_SECONDS`, so the lease
      it was checking had expired on its own, with nothing evicted at all.

    Holding a turn open for longer than the lease is not a situation the design has, and
    a test that has to create one is testing the harness. The rule itself is about the
    registry, so it is asserted there: no threads, no HTTP, no clock.
    """

    @staticmethod
    def registry_with(store: Store, capacity: int, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setenv("RSA_SESSION_CACHE_SIZE", str(capacity))
        monkeypatch.setattr(api_module, "make_chat_fn", lambda settings, config: _completion)
        return SessionRegistry(Settings(), ModelConfig(), store=store)  # type: ignore[call-arg]

    def test_a_session_holding_its_turn_is_kept_over_capacity(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dropping an agent under a running turn releases the lease that says this
        process is driving it, and leaves the turn writing into an object the next
        message cannot reach. Being briefly over capacity is the smaller problem."""
        registry = self.registry_with(store, 1, monkeypatch)
        who = Identity(subject="", user_id="ava_moore_2033")

        running = new_session()
        registry.begin_turn(running, who)  # takes the turn and does not give it back
        try:
            for _ in range(3):
                pressing = new_session()
                registry.begin_turn(pressing, who)
                registry.end_turn(pressing)

            assert running in registry._agents, "the session mid-turn was evicted"
            assert store.claim_owner(running) == registry.owner
        finally:
            registry.end_turn(running)

    def test_it_is_evicted_once_its_turn_ends(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: held over capacity while busy, not held for ever."""
        registry = self.registry_with(store, 1, monkeypatch)
        who = Identity(subject="", user_id="ava_moore_2033")

        running = new_session()
        registry.begin_turn(running, who)
        registry.end_turn(running)

        for _ in range(3):
            pressing = new_session()
            registry.begin_turn(pressing, who)
            registry.end_turn(pressing)

        assert running not in registry._agents
        assert len(registry._agents) == 1


class TestDroppingASessionGivesUpItsLease:
    def test_an_evicted_session_is_not_still_claimed(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        """A process holding the lease on a session it no longer has in memory is exactly
        the state the lease exists to rule out: another process would be refused a
        session nobody is driving."""
        with TestClient(app_with(1)) as client:
            evicted = new_session()
            say(client, evicted, "one")
            say(client, new_session(), "pushes the first one out")

        assert store.claim_owner(evicted) is None

    def test_reset_all_gives_up_every_lease(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        sessions = [new_session() for _ in range(3)]
        with TestClient(app_with(10)) as client:
            for session_id in sessions:
                say(client, session_id, "one")
            dropped = client.post("/sessions/reset").json()

        assert dropped["cleared"] == 3
        for session_id in sessions:
            assert store.claim_owner(session_id) is None


class TestTheLocksAreCleanedUpToo:
    def test_a_dropped_session_does_not_leave_its_turn_lock_behind(
        self, app_with: Any, store: Store, answers: list[Any]
    ) -> None:
        """A lock per session id, never removed, is a second unbounded structure keyed on
        a value the caller supplies. Small on its own, and the same leak as the one this
        change exists to close.
        """
        app = app_with(2)
        registry = api_module_registry(app)
        with TestClient(app) as client:
            for _ in range(12):
                say(client, new_session(), "hello")
            time.sleep(0.05)

        assert len(registry._turns) <= 2
        assert len(registry._agents) <= 2


def api_module_registry(app: Any) -> Any:
    """The registry the app was built with.

    Reached through the closure rather than stored on `app.state`, because putting it
    there for a test would change the application to suit the test.
    """
    for route in app.routes:
        closure = getattr(getattr(route, "endpoint", None), "__closure__", None) or ()
        for cell in closure:
            if type(cell.cell_contents).__name__ == "SessionRegistry":
                return cell.cell_contents
    raise AssertionError("no registry found on the app")
