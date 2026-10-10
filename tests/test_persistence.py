"""Application state in PostgreSQL: migrations, transactions, idempotency.

These tests need a real PostgreSQL. SQLite is not a substitute: what has to be shown is
that partial unique indexes, conflict semantics and transaction rollback hold on the
database production uses, and a green run on another dialect shows none of that.

Locally: `docker compose up -d db`, then point `RSA_TEST_DATABASE_URL` at it.
In CI: a postgres service in the workflow. With neither, these skip rather than pass
vacuously.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.guardrails import ProposedCall
from retail_support_agent.store import Store, idempotency_key

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

# Skip without a database locally, but fail in CI. A silent skip looks identical to a
# pass in the summary line, so CI would stay green while nothing was verified.
if not TEST_URL and os.getenv("CI"):
    raise RuntimeError(
        "CI must provide RSA_TEST_DATABASE_URL. Skipping these means none of the M1 "
        "acceptance checks ran while CI still reports green."
    )

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    """A clean set of tables per module.

    Uses `create_all` rather than alembic: the migration has its own test at the end of
    this file, and keeping the two separate makes it clear which one is red.
    """
    assert TEST_URL
    engine = make_engine(TEST_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


def new_session(store: Store) -> str:
    session_id = f"s-{uuid.uuid4().hex[:12]}"
    store.ensure_session(session_id, "ava_moore_2033")
    return session_id


CANCEL = ProposedCall("cancel_pending_order", {"order_id": "#W001", "reason": "no longer needed"})


class TestSessions:
    def test_ensure_is_idempotent_and_does_not_change_identity(self, store: Store) -> None:
        """Re-creating a session must not change its identity, which would mean a
        session switching users mid-conversation."""
        session_id = new_session(store)
        store.ensure_session(session_id, "someone_else_9999")
        assert store.session_user(session_id) == "ava_moore_2033"


class TestMessages:
    def test_same_seq_is_not_inserted_twice(self, store: Store) -> None:
        """A resent turn must not become two rows.

        The `(session_id, seq)` unique constraint stops it, not an application-level
        existence check: under concurrency two requests can both see nothing.
        """
        session_id = new_session(store)
        assert store.append_message(session_id, 1, "user", "cancel order #W001")
        assert not store.append_message(session_id, 1, "user", "cancel order #W001")
        assert len(store.messages(session_id)) == 1

    def test_next_seq_counts_from_what_is_stored(self, store: Store) -> None:
        session_id = new_session(store)
        assert store.next_seq(session_id) == 1
        store.append_message(session_id, 1, "user", "hello")
        assert store.next_seq(session_id) == 2


class TestConfirmations:
    def test_only_one_pending_per_session(self, store: Store) -> None:
        """Changed arguments are a new proposal, so the old pending row must be cancelled.

        Two pending rows would make "which one did the user just confirm" a guess.
        """
        session_id = new_session(store)
        store.open_confirmation(session_id, CANCEL)
        widened = ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "x"})
        store.open_confirmation(session_id, widened)

        pending = store.pending_confirmation(session_id)
        assert pending is not None
        assert pending.fingerprint == widened.fingerprint

    def test_confirmation_does_not_carry_to_other_arguments(self, store: Store) -> None:
        """The user agreed to the arguments they were shown; another set needs asking again."""
        session_id = new_session(store)
        store.open_confirmation(session_id, CANCEL)
        other = ProposedCall("cancel_pending_order", {"order_id": "#W999", "reason": "x"})
        assert store.confirm(session_id, other.fingerprint) is None
        # After the rejection the original row is still pending, not cancelled in passing.
        assert store.pending_confirmation(session_id) is not None

    def test_confirming_the_same_fingerprint_works(self, store: Store) -> None:
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        assert store.confirm(session_id, CANCEL.fingerprint) == cid
        assert store.pending_confirmation(session_id) is None

    def test_reopening_the_same_proposal_adds_no_row(self, store: Store) -> None:
        """M9: the MCP server asks again on every elicitation round. A new row each time
        would cancel the confirmation the user is in the middle of answering."""
        session_id = new_session(store)
        first = store.open_confirmation(session_id, CANCEL)
        again = store.open_confirmation(session_id, CANCEL)
        assert again == first
        assert store.confirm(session_id, CANCEL.fingerprint) == first

    def test_racing_openings_of_one_proposal_leave_one_row(self, store: Store) -> None:
        session_id = new_session(store)
        barrier = threading.Barrier(8)
        ids: list[int] = []
        errors: list[BaseException] = []

        def open_it() -> None:
            barrier.wait()
            try:
                ids.append(store.open_confirmation(session_id, CANCEL))
            except BaseException as exc:  # pragma: no cover - the assertion reports it
                errors.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(set(ids)) == 1

    def test_the_confirmation_of_one_argument_set_is_found_by_its_fingerprint(
        self, store: Store
    ) -> None:
        """Not the latest confirmation of any action: that would give a retry of an
        earlier action a different idempotency key from its first run."""
        session_id = new_session(store)
        first = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        other = ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "x"})
        second = store.open_confirmation(session_id, other)
        store.confirm(session_id, other.fingerprint)

        assert store.last_confirmation_id(session_id) == second
        assert store.confirmation_for(session_id, CANCEL.fingerprint) == first
        assert store.confirmation_for(session_id, other.fingerprint) == second
        never = ProposedCall("cancel_pending_order", {"order_id": "#W003", "reason": "x"})
        assert store.confirmation_for(session_id, never.fingerprint) is None

    def test_a_declined_proposal_is_owed_nothing(self, store: Store) -> None:
        """M10: an answered form must not keep the service from counting as idle."""
        session_id = new_session(store)
        before = store.pending_confirmation_count()
        cid = store.open_confirmation(session_id, CANCEL)
        assert store.pending_confirmation_count() == before + 1
        assert store.withdraw_confirmation(session_id, CANCEL.fingerprint) == cid
        assert store.pending_confirmation(session_id) is None
        assert store.pending_confirmation_count() == before
        # Withdrawn is not confirmed: it authorizes nothing.
        assert store.confirmation_for(session_id, CANCEL.fingerprint) is None
        assert CANCEL.fingerprint not in store.confirmed_fingerprints(session_id)

    def test_withdrawing_another_proposal_leaves_the_pending_one(self, store: Store) -> None:
        session_id = new_session(store)
        store.open_confirmation(session_id, CANCEL)
        other = ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "x"})
        assert store.withdraw_confirmation(session_id, other.fingerprint) is None
        pending = store.pending_confirmation(session_id)
        assert pending is not None and pending.fingerprint == CANCEL.fingerprint

    def test_asking_again_after_a_decline_is_a_new_question(self, store: Store) -> None:
        session_id = new_session(store)
        first = store.open_confirmation(session_id, CANCEL)
        store.withdraw_confirmation(session_id, CANCEL.fingerprint)
        again = store.open_confirmation(session_id, CANCEL)
        assert again != first
        assert store.confirm(session_id, CANCEL.fingerprint) == again

    def test_withdrawing_with_nothing_pending_does_nothing(self, store: Store) -> None:
        session_id = new_session(store)
        assert store.withdraw_confirmation(session_id, CANCEL.fingerprint) is None


class TestIdempotency:
    def test_the_same_key_executes_exactly_once(self, store: Store) -> None:
        """A retry must not execute the write again, which is the problem M1 exists to
        solve.

        The M0.6 guard guarantees that a write is confirmed first, but not that a
        confirmed write runs only once: a client timeout and resend would cancel the
        order twice.
        """
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        calls: list[int] = []

        def apply() -> str:
            calls.append(1)
            return "order cancelled"

        first = store.execute_once(session_id, CANCEL, cid, apply)
        for _ in range(4):
            again = store.execute_once(session_id, CANCEL, cid, apply)
            assert again.replayed
            assert again.result == first.result
            assert again.operation_id == first.operation_id

        assert len(calls) == 1, "the write ran more than once"
        assert len(store.operations(session_id)) == 1

    def test_a_new_confirmation_is_a_new_authorization(self, store: Store) -> None:
        """The same arguments confirmed again should run again.

        The user may genuinely want to return another identical item. Including
        confirmation_id in the key separates a retry from a second authorization;
        without it the second legitimate request would be swallowed silently.
        """
        session_id = new_session(store)
        first_cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        store.execute_once(session_id, CANCEL, first_cid, lambda: "first")

        second_cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        outcome = store.execute_once(session_id, CANCEL, second_cid, lambda: "second")

        assert not outcome.replayed
        assert outcome.result == "second"
        assert first_cid != second_cid

    def test_different_arguments_get_different_keys(self, store: Store) -> None:
        other = ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "x"})
        assert idempotency_key("s1", CANCEL, 1) != idempotency_key("s1", other, 1)
        # The session is part of the key, so two sessions do not interfere.
        assert idempotency_key("s1", CANCEL, 1) != idempotency_key("s2", CANCEL, 1)


class TestTransaction:
    def test_a_failing_apply_leaves_nothing_behind(self, store: Store) -> None:
        """When the business action fails, the operation row must disappear with it.

        A "logged but not executed" record is worse than no record: an audit would
        conclude it happened.
        """
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)

        def boom() -> str:
            raise RuntimeError("the domain environment refused the change")

        with pytest.raises(RuntimeError):
            store.execute_once(session_id, CANCEL, cid, boom)

        assert store.operations(session_id) == []
        assert store.audit_events(session_id) == []

    def test_a_failed_attempt_does_not_burn_the_key(self, store: Store) -> None:
        """After a failure the same key must still be usable.

        If a failure claimed the key, one transient fault would make that operation
        permanently unexecutable while reporting "already done", which is the hardest
        kind of failure to diagnose.
        """
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)

        with pytest.raises(RuntimeError):
            store.execute_once(session_id, CANCEL, cid, _raise)

        outcome = store.execute_once(session_id, CANCEL, cid, lambda: "worked this time")
        assert not outcome.replayed
        assert outcome.result == "worked this time"


class TestConcurrency:
    def test_concurrent_submissions_execute_once(self, store: Store) -> None:
        """Concurrent submissions of one key execute once; the rest return that result.

        This is why check-then-insert is not enough: two threads can both see nothing.
        The unique index is what stops it.
        """
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)

        executed: list[int] = []
        lock = threading.Lock()
        results: list[str] = []
        barrier = threading.Barrier(8)

        def apply() -> str:
            with lock:
                executed.append(1)
            return "order cancelled"

        def worker() -> None:
            barrier.wait()
            outcome = store.execute_once(session_id, CANCEL, cid, apply)
            with lock:
                results.append(outcome.result)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(executed) == 1, f"the write ran {len(executed)} times"
        assert results == ["order cancelled"] * 8
        assert len(store.operations(session_id)) == 1


class TestAudit:
    def test_every_executed_write_leaves_an_audit_trail(self, store: Store) -> None:
        """The audit must answer who authorized which write action, and when."""
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        store.execute_once(session_id, CANCEL, cid, lambda: "order cancelled")

        events = store.audit_events(session_id)
        assert len(events) == 1
        assert events[0].kind == "write_executed"
        assert events[0].detail["fingerprint"] == CANCEL.fingerprint
        assert events[0].detail["confirmation_id"] == cid


class TestMigrations:
    def test_upgrade_and_downgrade_both_run(self) -> None:
        """Migrations must go both ways.

        Up-only means the first failed deployment has no way back, and writing the
        downgrade at that point is too late.
        """
        alembic = pytest.importorskip("alembic.command", reason="alembic is not installed")
        from alembic.config import Config

        assert TEST_URL
        # Run in a separate schema so the tables used by the tests above survive.
        engine = make_engine(TEST_URL)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS migration_check CASCADE"))
            conn.execute(text("CREATE SCHEMA migration_check"))
        engine.dispose()

        config = Config("alembic.ini")
        url = f"{TEST_URL}?options=-csearch_path%3Dmigration_check"
        old = os.environ.get("RSA_DATABASE_URL")
        os.environ["RSA_DATABASE_URL"] = url
        try:
            alembic.upgrade(config, "head")
            alembic.downgrade(config, "base")
        finally:
            if old is None:
                os.environ.pop("RSA_DATABASE_URL", None)
            else:
                os.environ["RSA_DATABASE_URL"] = old
            engine = make_engine(TEST_URL)
            with engine.begin() as conn:
                conn.execute(text("DROP SCHEMA IF EXISTS migration_check CASCADE"))
            engine.dispose()


def _raise() -> str:
    raise RuntimeError("transient fault")
