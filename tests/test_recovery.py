"""M2: rebuilding a session after a restart, and one session having one owner.

These tests need a real PostgreSQL for the same reason M1's do: the claim is an
`INSERT ... ON CONFLICT DO UPDATE ... WHERE`, and whether it takes the lease is a
property of PostgreSQL, not of the application code. With no database they skip rather
than pass vacuously.

Locally: `docker compose up -d db`, then point `RSA_TEST_DATABASE_URL` at it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from retail_support_agent.agent import RetailAgent
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.domain import DomainError, ToolResult, ensure_data_dir
from retail_support_agent.guardrails import ProposedCall, SessionLimits, SessionState
from retail_support_agent.store import Store

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

CANCEL = ProposedCall("cancel_pending_order", {"order_id": "#W001", "reason": "no longer needed"})


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    """A clean set of tables per module, on a real PostgreSQL.

    The requirement lives on the fixture rather than on the module, because the replay
    tests need the domain and not the database, and a module-level skip would take them
    down with it - including in the CI job that is the only one able to run them.
    """
    if not TEST_URL:
        if os.getenv("CI"):
            raise RuntimeError(
                "CI must provide RSA_TEST_DATABASE_URL for the job running these tests. "
                "Skipping them means none of the M2 database thresholds ran while CI "
                "still reports green."
            )
        pytest.skip("needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (docker compose up -d db)")
    engine = make_engine(TEST_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


def new_session(store: Store) -> str:
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    store.ensure_session(session_id, "alice")
    return session_id


# ---- doubles ------------------------------------------------------------


class StatefulDomain:
    """A domain whose writes actually change state, so a replay can be checked.

    `RetailDomain` is exercised separately in TestReplayReproducesTheRealDatabase; this
    double keeps the agent-level tests offline and free.
    """

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    @property
    def policy(self) -> str:
        return "(test policy)"

    def tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def tool_names(self) -> set[str]:
        return {"get_order_details", "cancel_pending_order"}

    def order_owner(self, order_id: str) -> str | None:
        return "alice"

    def order_status(self, order_id: str) -> str | None:
        return "pending"

    def db_hash(self) -> str:
        return json.dumps(sorted(self.cancelled))

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "cancel_pending_order":
            self.cancelled.append(str(arguments["order_id"]))
        return ToolResult(name=name, ok=True, content=json.dumps({"status": "cancelled"}))


class SilentModel:
    """A model that is never expected to be called during recovery."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self.calls += 1
        raise AssertionError("recovery must not call the model")


def build_agent(store: Store, session_id: str, domain: StatefulDomain) -> RetailAgent:
    state = SessionState(user_id="alice", limits=SessionLimits())
    return RetailAgent(
        SilentModel(),
        domain,  # type: ignore[arg-type]
        state,
        store=store,
        session_id=session_id,
    )


def real_domain() -> None:
    """Skip unless the real domain is usable.

    Two conditions, and the order matters. The data checkout has to exist, so an
    environment that has not fetched upstream skips instead of failing. And
    `ensure_data_dir()` has to run before tau2 is imported, because upstream resolves its
    data directory at import time: importorskip on its own would import tau2 pointing at
    the packaged data, which holds none, and every later use in the process would fail.

    CI runs these in the `upstream-pin` job, which has both the checkout and the bench
    group. The static job has neither, and would skip them silently.
    """
    try:
        ensure_data_dir()
    except DomainError as exc:
        pytest.skip(str(exc))
    pytest.importorskip("tau2", reason="the domain needs the bench group")


# ---- acceptance thresholds ----------------------------------------------


class TestReplayReproducesTheRealDatabase:
    """Threshold 1: after a restart the resumed session's db_hash equals the old one.

    This is the one test that uses the real τ²-bench domain, because the claim being
    checked is about upstream's data and tools, not about our double.
    """

    def test_a_fresh_domain_replayed_matches_the_original(self) -> None:
        real_domain()
        from retail_support_agent.domain import RetailDomain

        order_id = "#W4135875"  # pending order of the demo identity ava_moore_2033

        original = RetailDomain()
        before = original.db_hash()
        original.execute(
            "cancel_pending_order", {"order_id": order_id, "reason": "no longer needed"}
        )
        after = original.db_hash()
        assert before != after, "the write must actually change the database"

        # A fresh process: new environment, same initial data, the same write replayed.
        replayed = RetailDomain()
        assert replayed.db_hash() == before
        replayed.execute(
            "cancel_pending_order", {"order_id": order_id, "reason": "no longer needed"}
        )

        assert replayed.db_hash() == after

    def test_replay_order_matters_and_is_preserved(self) -> None:
        """Operations are replayed in insertion order.

        Writes are not commutative in general - cancelling and then modifying is not the
        same as the reverse - so a recovery that replayed them in an arbitrary order
        could produce a different database while reporting success.
        """
        real_domain()
        from retail_support_agent.domain import RetailDomain

        domain = RetailDomain()
        domain.execute(
            "modify_user_address",
            {
                "user_id": "ava_moore_2033",
                "address1": "1 First St",
                "address2": "",
                "city": "Denver",
                "state": "CO",
                "country": "USA",
                "zip": "80201",
            },
        )
        domain.execute(
            "modify_user_address",
            {
                "user_id": "ava_moore_2033",
                "address1": "2 Second St",
                "address2": "",
                "city": "Denver",
                "state": "CO",
                "country": "USA",
                "zip": "80202",
            },
        )
        forward = domain.db_hash()

        reversed_domain = RetailDomain()
        reversed_domain.execute(
            "modify_user_address",
            {
                "user_id": "ava_moore_2033",
                "address1": "2 Second St",
                "address2": "",
                "city": "Denver",
                "state": "CO",
                "country": "USA",
                "zip": "80202",
            },
        )
        reversed_domain.execute(
            "modify_user_address",
            {
                "user_id": "ava_moore_2033",
                "address1": "1 First St",
                "address2": "",
                "city": "Denver",
                "state": "CO",
                "country": "USA",
                "zip": "80201",
            },
        )

        assert reversed_domain.db_hash() != forward


class TestRecoveryDoesNotReExecute:
    """Threshold 2: recovery adds no operations row and no audit row."""

    def test_replay_writes_no_new_rows(self, store: Store) -> None:
        session_id = new_session(store)
        confirmation_id = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        store.execute_once(session_id, CANCEL, confirmation_id, lambda: "cancelled")

        operations_before = len(store.operations(session_id))
        audit_before = len(store.audit_events(session_id))

        domain = StatefulDomain()
        report = build_agent(store, session_id, domain).recover()

        assert report.operations_replayed == 1
        assert domain.cancelled == ["#W001"]
        assert len(store.operations(session_id)) == operations_before
        assert len(store.audit_events(session_id)) == audit_before

    def test_a_failed_operation_is_not_replayed(self, store: Store) -> None:
        """A write that rolled back left no row, so there is nothing to replay.

        Recorded as its own test because the opposite behaviour - replaying a failed
        write - would apply a change the user never saw succeed.
        """
        session_id = new_session(store)
        with pytest.raises(RuntimeError):
            store.execute_once(session_id, CANCEL, None, _raise)

        domain = StatefulDomain()
        report = build_agent(store, session_id, domain).recover()

        assert report.operations_replayed == 0
        assert domain.cancelled == []


class TestConfirmationSurvivesARestart:
    """Threshold 3: a pending confirmation is still pending, and answering yes executes
    the write exactly once."""

    def test_pending_confirmation_is_restored(self, store: Store) -> None:
        session_id = new_session(store)
        store.open_confirmation(session_id, CANCEL)

        agent = build_agent(store, session_id, StatefulDomain())
        report = agent.recover()

        assert report.pending_confirmation
        pending = agent._session.pending
        assert pending is not None
        assert pending.fingerprint == CANCEL.fingerprint

    def test_an_already_confirmed_write_is_not_asked_again(self, store: Store) -> None:
        """The user authorized this argument set before the restart.

        Asking again would be safe but wrong: it makes the system look like it forgot,
        and the M0.6 measurements show every extra confirmation turn costs a round trip
        and can lose the task.
        """
        session_id = new_session(store)
        store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)

        agent = build_agent(store, session_id, StatefulDomain())
        agent.recover()

        assert CANCEL.fingerprint in agent._session.confirmed_fingerprints

    def test_the_write_still_runs_only_once_after_recovery(self, store: Store) -> None:
        """The key includes confirmation_id, so recovery must restore that same id.

        With a different id the key differs, the unique index does not fire, and the
        write executes a second time - the exact failure M1 exists to prevent.
        """
        session_id = new_session(store)
        confirmation_id = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        store.execute_once(session_id, CANCEL, confirmation_id, lambda: "cancelled")

        agent = build_agent(store, session_id, StatefulDomain())
        agent.recover()
        assert agent._confirmation_id == confirmation_id

        outcome = store.execute_once(session_id, CANCEL, agent._confirmation_id, _raise)
        assert outcome.replayed
        assert len(store.operations(session_id)) == 1


class TestStopConditionsResume:
    """Threshold 4: restarting does not reset the limits."""

    def test_counters_are_restored(self, store: Store) -> None:
        session_id = new_session(store)
        store.save_progress(session_id, steps=18, tool_calls=14)

        agent = build_agent(store, session_id, StatefulDomain())
        agent.recover()

        assert agent._session.steps == 18
        assert agent._session.tool_calls == 14

    def test_a_restart_cannot_reset_a_reached_limit(self, store: Store) -> None:
        """Without this, restarting is a way around the stop conditions."""
        session_id = new_session(store)
        store.save_progress(session_id, steps=20, tool_calls=0)

        agent = build_agent(store, session_id, StatefulDomain())
        agent.recover()

        assert agent._session.stop_reason() is not None

    def test_the_time_already_spent_working_resumes(self, store: Store) -> None:
        """Otherwise a restart hands the session a fresh time budget, which is the same
        hole `test_a_restart_cannot_reset_a_reached_limit` closes for the counters."""
        session_id = new_session(store)
        store.save_progress(session_id, steps=0, tool_calls=0, worked_seconds=42.0)

        agent = build_agent(store, session_id, StatefulDomain())
        agent.recover()

        assert agent._session.elapsed() == pytest.approx(42.0)

    def test_an_old_session_that_never_worked_still_has_its_budget(self, store: Store) -> None:
        """The production defect. The limit used to be read as the age of the session
        row, and an agent's case session is reached again by an id derived from the
        ticket and the agent - so a case opened hours ago was dead on its next message,
        with no way to obtain a fresh session for it.
        """
        session_id = new_session(store)
        time.sleep(0.05)

        agent = build_agent(store, session_id, StatefulDomain())
        agent.recover()

        assert agent._session.elapsed() == 0.0
        assert agent._session.stop_reason() is None


class TestOneOwnerPerSession:
    """Threshold 5: two processes claiming one session, exactly one proceeds."""

    def test_a_second_owner_is_refused(self, store: Store) -> None:
        session_id = new_session(store)
        assert store.claim_session(session_id, "process-a", 60.0)
        assert not store.claim_session(session_id, "process-b", 60.0)
        assert store.claim_owner(session_id) == "process-a"

    def test_the_holder_can_renew(self, store: Store) -> None:
        """Renewal has to succeed, or a process would lose its own session mid-turn."""
        session_id = new_session(store)
        assert store.claim_session(session_id, "process-a", 60.0)
        assert store.claim_session(session_id, "process-a", 60.0)

    def test_an_expired_lease_is_taken_over(self, store: Store) -> None:
        """A process that crashes mid-turn never releases its lease.

        Without an expiry that session would be unusable until someone deleted the row
        by hand, which is a worse failure than the interleaving the lease prevents.
        """
        session_id = new_session(store)
        assert store.claim_session(session_id, "process-a", 0.0)
        assert store.claim_owner(session_id) is None
        assert store.claim_session(session_id, "process-b", 60.0)
        assert store.claim_owner(session_id) == "process-b"

    def test_release_is_ignored_from_a_non_holder(self, store: Store) -> None:
        """Releasing a lease we do not hold would hand the session to a third process
        while its real owner is still working."""
        session_id = new_session(store)
        store.claim_session(session_id, "process-a", 60.0)
        store.release_session(session_id, "process-b")
        assert store.claim_owner(session_id) == "process-a"

    def test_release_frees_the_session(self, store: Store) -> None:
        session_id = new_session(store)
        store.claim_session(session_id, "process-a", 60.0)
        store.release_session(session_id, "process-a")
        assert store.claim_session(session_id, "process-b", 60.0)


class TestConversationIsRestored:
    def test_visible_turns_come_back_in_order(self, store: Store) -> None:
        session_id = new_session(store)
        store.append_message(session_id, 1, "user", "cancel #W001")
        store.append_message(session_id, 2, "assistant", "Please confirm.")

        agent = build_agent(store, session_id, StatefulDomain())
        report = agent.recover()

        assert report.messages == 2
        # The first message is always the system prompt, which is rebuilt, not stored.
        assert [m["role"] for m in agent._messages] == ["system", "user", "assistant"]
        assert agent._messages[1]["content"] == "cancel #W001"


class TestDegradationWithoutADatabase:
    """Threshold 7: with no database the behaviour is M0's in-memory sessions."""

    def test_recover_is_a_no_op(self) -> None:
        state = SessionState(user_id="alice", limits=SessionLimits())
        agent = RetailAgent(SilentModel(), StatefulDomain(), state)  # type: ignore[arg-type]

        report = agent.recover()

        assert not report.recovered
        assert report.operations_replayed == 0


def _raise() -> str:
    raise RuntimeError("the write failed")


class TestTheApiRefusesAContestedSession:
    """Threshold 5 at the HTTP boundary: a session another process holds is refused
    rather than served from a second, diverging copy."""

    def test_chat_returns_409_when_another_process_holds_the_session(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from retail_support_agent.api import create_app
        from retail_support_agent.config import Settings

        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        session_id = new_session(store)
        store.claim_session(session_id, "another-process", 60.0)

        client = TestClient(create_app(settings=Settings(), store=store))  # type: ignore[call-arg]
        response = client.post("/chat", json={"session_id": session_id, "message": "hello"})

        # 409, not 503: retrying elsewhere will not help, and a 5xx invites a retry loop.
        assert response.status_code == 409
        assert session_id in response.json()["detail"]

    def test_releasing_a_session_lets_another_process_take_it(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`/session/{id}/reset` has to drop the lease as well as the in-process agent.

        Keeping the lease would leave the session unusable by anyone until it expired,
        while the endpoint reported success.
        """
        from retail_support_agent.api import SessionRegistry
        from retail_support_agent.config import Settings
        from retail_support_agent.llm import ModelConfig

        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        session_id = new_session(store)
        # M3 moved the identity from the constructor to the request: the registry no
        # longer holds one user id for every session.
        registry = SessionRegistry(
            Settings(),  # type: ignore[call-arg]
            ModelConfig(),
            store=store,
            owner="process-a",
        )
        store.claim_session(session_id, "process-a", 60.0)
        registry.reset(session_id)

        assert store.claim_session(session_id, "process-b", 60.0)
