"""应用侧状态落库：迁移、事务、幂等。

**这些用例需要一个真的 PostgreSQL。** 不用 SQLite 顶替：这一步要证明的正是
部分唯一索引、`ON CONFLICT` 语义和事务回滚在**生产用的那个数据库**上成立，
换个方言测出来的绿色说明不了这件事。

本地：`docker compose up -d db`，然后 `RSA_TEST_DATABASE_URL` 指向它。
CI：workflow 里起一个 postgres service。都没有时跳过——而不是假装通过。
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

# 本机没库就跳过；**CI 上没库就报错**。
# 静默跳过在汇总行里和"通过"长得一模一样——真出问题时 CI 依然是绿的。
if not TEST_URL and os.getenv("CI"):
    raise RuntimeError(
        "CI 上必须提供 RSA_TEST_DATABASE_URL。跳过持久化用例等于 M1 的验收一条没跑，"
        "而 CI 还是绿的。"
    )

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="需要真的 PostgreSQL：设置 RSA_TEST_DATABASE_URL（本地可用 docker compose up -d db）",
)


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    """每个模块用一套干净的表。

    直接 `create_all` 而不是跑 alembic：迁移本身另有用例验证（见文件末尾），
    这里要测的是行为，两件事分开测才知道红的是哪一个。
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
        """重复建同一个会话不该换掉它的身份——那等于让一次会话中途改换用户。"""
        session_id = new_session(store)
        store.ensure_session(session_id, "someone_else_9999")
        assert store.session_user(session_id) == "ava_moore_2033"


class TestMessages:
    def test_same_seq_is_not_inserted_twice(self, store: Store) -> None:
        """客户端重发同一轮，不能变成两条记录。

        拦住它的是 `(session_id, seq)` 唯一约束，不是应用层"先查有没有"——
        并发下两个请求可以同时查到"没有"。
        """
        session_id = new_session(store)
        assert store.append_message(session_id, 1, "user", "取消订单 #W001")
        assert not store.append_message(session_id, 1, "user", "取消订单 #W001")
        assert len(store.messages(session_id)) == 1

    def test_next_seq_counts_from_what_is_stored(self, store: Store) -> None:
        session_id = new_session(store)
        assert store.next_seq(session_id) == 1
        store.append_message(session_id, 1, "user", "你好")
        assert store.next_seq(session_id) == 2


class TestConfirmations:
    def test_only_one_pending_per_session(self, store: Store) -> None:
        """模型改了参数就是重新提议，旧的那条必须作废。

        两条 pending 并存会让"用户刚才确认的是哪一条"变成猜谜。
        """
        session_id = new_session(store)
        store.open_confirmation(session_id, CANCEL)
        widened = ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "x"})
        store.open_confirmation(session_id, widened)

        pending = store.pending_confirmation(session_id)
        assert pending is not None
        assert pending.fingerprint == widened.fingerprint

    def test_confirmation_does_not_carry_to_other_arguments(self, store: Store) -> None:
        """用户同意的是当时列给他看的那组参数，换一组就得重新问。"""
        session_id = new_session(store)
        store.open_confirmation(session_id, CANCEL)
        other = ProposedCall("cancel_pending_order", {"order_id": "#W999", "reason": "x"})
        assert store.confirm(session_id, other.fingerprint) is None
        # 拒绝之后那条仍然是 pending，不会被顺手作废
        assert store.pending_confirmation(session_id) is not None

    def test_confirming_the_same_fingerprint_works(self, store: Store) -> None:
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        assert store.confirm(session_id, CANCEL.fingerprint) == cid
        assert store.pending_confirmation(session_id) is None


class TestIdempotency:
    def test_the_same_key_executes_exactly_once(self, store: Store) -> None:
        """重试不该重复执行写操作——M1 要解决的核心问题就是这一条。

        M0.6 的护栏保证了"写操作必须先确认"，但没有保证"确认过的写操作只
        执行一次"：客户端超时重发，订单会被取消第二次。
        """
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        calls: list[int] = []

        def apply() -> str:
            calls.append(1)
            return "订单已取消"

        first = store.execute_once(session_id, CANCEL, cid, apply)
        for _ in range(4):
            again = store.execute_once(session_id, CANCEL, cid, apply)
            assert again.replayed
            assert again.result == first.result
            assert again.operation_id == first.operation_id

        assert len(calls) == 1, "写操作被执行了不止一次"
        assert len(store.operations(session_id)) == 1

    def test_a_new_confirmation_is_a_new_authorization(self, store: Store) -> None:
        """同一组参数**再次获得确认**应当能再执行一次。

        用户可能真的想再退一件一样的商品。幂等键带上 confirmation_id 就是为了
        区分"重试"和"第二次授权"——不带的话，第二次合法请求会被静默吞掉。
        """
        session_id = new_session(store)
        first_cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        store.execute_once(session_id, CANCEL, first_cid, lambda: "第一次")

        second_cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        outcome = store.execute_once(session_id, CANCEL, second_cid, lambda: "第二次")

        assert not outcome.replayed
        assert outcome.result == "第二次"
        assert first_cid != second_cid

    def test_different_arguments_get_different_keys(self, store: Store) -> None:
        other = ProposedCall("cancel_pending_order", {"order_id": "#W002", "reason": "x"})
        assert idempotency_key("s1", CANCEL, 1) != idempotency_key("s1", other, 1)
        # 会话也进键：两个会话各自的同一操作互不干扰
        assert idempotency_key("s1", CANCEL, 1) != idempotency_key("s2", CANCEL, 1)


class TestTransaction:
    def test_a_failing_apply_leaves_nothing_behind(self, store: Store) -> None:
        """业务动作失败时，操作记录必须跟着一起消失。

        留下"记了操作但没执行"的痕迹比不记还糟：事后审计会认定它发生过。
        """
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)

        def boom() -> str:
            raise RuntimeError("领域环境拒绝了这次修改")

        with pytest.raises(RuntimeError):
            store.execute_once(session_id, CANCEL, cid, boom)

        assert store.operations(session_id) == []
        assert store.audit_events(session_id) == []

    def test_a_failed_attempt_does_not_burn_the_key(self, store: Store) -> None:
        """失败之后同一个键必须还能再试。

        如果失败也占住了幂等键，一次瞬时故障就会让这个操作**永远**执行不了，
        而且表现成"已经执行过了"——最难查的那种。
        """
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)

        with pytest.raises(RuntimeError):
            store.execute_once(session_id, CANCEL, cid, _raise)

        outcome = store.execute_once(session_id, CANCEL, cid, lambda: "这次成了")
        assert not outcome.replayed
        assert outcome.result == "这次成了"


class TestConcurrency:
    def test_concurrent_submissions_execute_once(self, store: Store) -> None:
        """同一幂等键并发提交，只有一个真正执行，其余取回同一结果。

        这条用例正是"不能用先查再插"的理由：两个线程可以同时查到"没有"。
        挡住它的是唯一索引。
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
            return "订单已取消"

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

        assert len(executed) == 1, f"写操作被执行了 {len(executed)} 次"
        assert results == ["订单已取消"] * 8
        assert len(store.operations(session_id)) == 1


class TestAudit:
    def test_every_executed_write_leaves_an_audit_trail(self, store: Store) -> None:
        """审计要能回答"谁在什么时候授权了哪一次写操作"。"""
        session_id = new_session(store)
        cid = store.open_confirmation(session_id, CANCEL)
        store.confirm(session_id, CANCEL.fingerprint)
        store.execute_once(session_id, CANCEL, cid, lambda: "订单已取消")

        events = store.audit_events(session_id)
        assert len(events) == 1
        assert events[0].kind == "write_executed"
        assert events[0].detail["fingerprint"] == CANCEL.fingerprint
        assert events[0].detail["confirmation_id"] == cid


class TestMigrations:
    def test_upgrade_and_downgrade_both_run(self) -> None:
        """迁移必须能上也能下。

        只能上不能下，等于第一次上线出问题就没有退路——那时候写 downgrade
        已经来不及了。
        """
        alembic = pytest.importorskip("alembic.command", reason="alembic 未安装")
        from alembic.config import Config

        assert TEST_URL
        # 用独立的 schema 跑，免得把上面那些用例的表删掉
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
    raise RuntimeError("瞬时故障")
