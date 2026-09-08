"""应用侧状态的读写。事务边界与幂等都在这里，不在调用方。

三条规则，其余都是细节：

1. **一次写操作 = 一个事务。** 操作记录、业务效果、审计事件要么一起成立，
   要么一起不存在。中途失败留下"记了操作但没执行"的痕迹，比不记还糟。
2. **幂等靠唯一索引，不靠先查再插。** `SELECT ... 没有就 INSERT` 在并发下
   必然漏：两个请求可以同时查到"没有"。
3. **确认的指纹与护栏同源。** 参数变了指纹就变，旧确认自动失效。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from retail_support_agent.db import (
    AuditEventRow,
    ConfirmationRow,
    MessageRow,
    OperationRow,
    SessionRow,
)
from retail_support_agent.guardrails import ProposedCall


def idempotency_key(session_id: str, call: ProposedCall, confirmation_id: int | None) -> str:
    """幂等键 = 会话 + 工具名 + 规范化参数 + 那一次确认。

    带上 confirmation_id 是有意的：同一组参数**再次获得确认**是一次新的授权，
    应当允许再执行一次（用户可能真的想再退一件同样的商品）。
    不带的话，第二次合法请求会被当成重试而被静默吞掉。
    """
    canonical = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)
    raw = f"{session_id}|{call.name}|{canonical}|{confirmation_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class OperationOutcome:
    """一次写操作的结果。`replayed=True` 表示这次没有真正执行，是取回的旧结果。"""

    operation_id: int
    result: str
    replayed: bool


class Store:
    """应用侧状态的仓储。每个方法自己开一个事务。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    # ── 会话 ────────────────────────────────────────────────
    def ensure_session(self, session_id: str, user_id: str) -> None:
        """会话不存在就建。已存在时**不覆盖 user_id**——那会悄悄换掉一次会话的身份。"""
        with self._sessions.begin() as db:
            if db.get(SessionRow, session_id) is None:
                db.add(SessionRow(id=session_id, user_id=user_id))

    def session_user(self, session_id: str) -> str | None:
        with self._sessions() as db:
            row = db.get(SessionRow, session_id)
            return row.user_id if row else None

    # ── 消息 ────────────────────────────────────────────────
    def append_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        content: str,
        tool_calls: list[str] | None = None,
    ) -> bool:
        """追加一轮消息。返回 False 表示这一轮已经存在（重发），没有插入第二条。"""
        try:
            with self._sessions.begin() as db:
                db.add(
                    MessageRow(
                        session_id=session_id,
                        seq=seq,
                        role=role,
                        content=content,
                        tool_calls=list(tool_calls or []),
                    )
                )
        except IntegrityError:
            return False
        return True

    def messages(self, session_id: str) -> list[MessageRow]:
        with self._sessions() as db:
            stmt = (
                select(MessageRow)
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.seq)
            )
            return list(db.scalars(stmt))

    def next_seq(self, session_id: str) -> int:
        with self._sessions() as db:
            stmt = (
                select(MessageRow.seq)
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.seq.desc())
                .limit(1)
            )
            last = db.scalar(stmt)
            return (last or 0) + 1

    # ── 确认 ────────────────────────────────────────────────
    def open_confirmation(self, session_id: str, call: ProposedCall) -> int:
        """登记一条待确认的写操作，返回它的 id。

        同一会话已有 pending 时，**新的取代旧的**：模型改了参数就等于重新提议，
        让两条 pending 并存会让"用户确认的是哪一条"变成猜谜。
        """
        with self._sessions.begin() as db:
            stmt = select(ConfirmationRow).where(
                ConfirmationRow.session_id == session_id,
                ConfirmationRow.state == "pending",
            )
            for stale in db.scalars(stmt):
                stale.state = "cancelled"
                stale.resolved_at = _utcnow(db)
            db.flush()
            row = ConfirmationRow(
                session_id=session_id,
                tool_name=call.name,
                fingerprint=call.fingerprint,
                arguments=dict(call.arguments),
                state="pending",
            )
            db.add(row)
            db.flush()
            return int(row.id)

    def confirm(self, session_id: str, fingerprint: str) -> int | None:
        """把 pending 的确认标成 confirmed，返回它的 id。

        指纹对不上就**不确认**并返回 None：用户同意的是当时列给他看的那组参数，
        模型换了参数就必须重新问一次。
        """
        with self._sessions.begin() as db:
            stmt = select(ConfirmationRow).where(
                ConfirmationRow.session_id == session_id,
                ConfirmationRow.state == "pending",
            )
            row = db.scalars(stmt).one_or_none()
            if row is None or row.fingerprint != fingerprint:
                return None
            row.state = "confirmed"
            row.resolved_at = _utcnow(db)
            return int(row.id)

    def pending_confirmation(self, session_id: str) -> ConfirmationRow | None:
        with self._sessions() as db:
            stmt = select(ConfirmationRow).where(
                ConfirmationRow.session_id == session_id,
                ConfirmationRow.state == "pending",
            )
            return db.scalars(stmt).one_or_none()

    # ── 写操作（幂等 + 事务） ─────────────────────────────────
    def execute_once(
        self,
        session_id: str,
        call: ProposedCall,
        confirmation_id: int | None,
        apply: Callable[[], str],
    ) -> OperationOutcome:
        """执行一次写操作，同一幂等键只会真正执行一次。

        顺序是**先占键、再执行、后提交**：

        1. 插入 operations 行占住幂等键。冲突 → 已经执行过，取回旧结果返回。
        2. 在同一个事务里调用 `apply()` 做真正的业务改动。
        3. 记审计事件，一起提交。

        `apply()` 抛异常时整个事务回滚——操作记录与业务效果同时不存在，
        不会留下"记了但没做"或"做了但没记"的中间态。

        这里刻意**不**用 `INSERT ... ON CONFLICT DO NOTHING`：那样拿不到
        "是不是我插进去的"这个信息，还得再查一次；捕获 IntegrityError 更直白。
        """
        key = idempotency_key(session_id, call, confirmation_id)

        existing = self._find_operation(key)
        if existing is not None:
            return OperationOutcome(existing[0], existing[1], replayed=True)

        try:
            with self._sessions.begin() as db:
                op = OperationRow(
                    session_id=session_id,
                    confirmation_id=confirmation_id,
                    idempotency_key=key,
                    tool_name=call.name,
                    arguments=dict(call.arguments),
                    result="",
                    status="succeeded",
                )
                db.add(op)
                db.flush()  # 这一步就会撞唯一索引，早于真正执行业务改动

                result = apply()
                op.result = result
                db.add(
                    AuditEventRow(
                        session_id=session_id,
                        operation_id=op.id,
                        kind="write_executed",
                        detail={
                            "tool": call.name,
                            "fingerprint": call.fingerprint,
                            "confirmation_id": confirmation_id,
                        },
                    )
                )
                return OperationOutcome(int(op.id), result, replayed=False)
        except IntegrityError:
            # 并发下另一个请求抢先占住了同一个键：它执行，我们取它的结果。
            found = self._find_operation(key)
            if found is None:  # pragma: no cover - 唯一索引冲突后必然能查到
                raise
            return OperationOutcome(found[0], found[1], replayed=True)

    def _find_operation(self, key: str) -> tuple[int, str] | None:
        with self._sessions() as db:
            stmt = select(OperationRow).where(OperationRow.idempotency_key == key)
            row = db.scalars(stmt).one_or_none()
            return (int(row.id), row.result) if row else None

    def operations(self, session_id: str) -> list[OperationRow]:
        with self._sessions() as db:
            stmt = (
                select(OperationRow)
                .where(OperationRow.session_id == session_id)
                .order_by(OperationRow.id)
            )
            return list(db.scalars(stmt))

    def audit_events(self, session_id: str) -> list[AuditEventRow]:
        with self._sessions() as db:
            stmt = (
                select(AuditEventRow)
                .where(AuditEventRow.session_id == session_id)
                .order_by(AuditEventRow.id)
            )
            return list(db.scalars(stmt))


def _utcnow(db: Session) -> Any:
    """取数据库的当前时间，而不是应用进程的。

    应用机器的时钟和数据库的时钟可以差出几秒；审计时间线以数据库为准，
    否则同一串事件的先后顺序可能自相矛盾。
    """
    from sqlalchemy import func

    return db.scalar(select(func.now()))
