"""写操作的硬性底线：归属校验、参数绑定确认、停止条件。

这三条是 DEVELOPMENT_PLAN.md M0.3 的"必需底线"，做成不依赖模型的纯逻辑：
模型可以胡说，但它改不了这里的判定。任何写操作都必须先过这一关。

确认必须**绑定到具体参数**。用户确认的是"取消订单 #W123"，不是"确认吗"；
模型若在确认后改了参数，指纹就对不上，必须重新确认。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from retail_support_agent.domain import WRITE_TOOLS

# 参数里可能出现订单号的字段名。用于归属校验。
ORDER_ID_FIELDS = ("order_id",)
USER_ID_FIELDS = ("user_id",)


class Decision(StrEnum):
    """守卫对一次工具调用的裁决。"""

    ALLOW = "allow"  # 读操作，或已确认且指纹匹配的写操作
    NEEDS_CONFIRMATION = "needs_confirmation"  # 写操作，尚未获得针对该参数的确认
    DENIED_OWNERSHIP = "denied_ownership"  # 订单不属于当前会话身份
    DENIED_UNKNOWN_TOOL = "denied_unknown_tool"  # 不在上游工具清单里


@dataclass(frozen=True)
class ProposedCall:
    """模型提议的一次工具调用。"""

    name: str
    arguments: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        """工具名 + 规范化参数的摘要。参数变了指纹就变，旧确认自动失效。"""
        canonical = json.dumps(self.arguments, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(f"{self.name}|{canonical}".encode()).hexdigest()

    def describe(self) -> str:
        """给用户看的确认文案，必须逐项列出真实参数，不能只说"确认吗"。"""
        if not self.arguments:
            return f"执行 {self.name}（无参数）"
        items = "\n".join(
            f"  - {key}: {json.dumps(value, ensure_ascii=False, default=str)}"
            for key, value in sorted(self.arguments.items())
        )
        return f"执行 {self.name}，参数如下：\n{items}"


@dataclass
class GuardVerdict:
    """裁决结果。被拒时 reason 会原样进入给用户的解释。"""

    decision: Decision
    reason: str = ""
    call: ProposedCall | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass
class SessionLimits:
    """停止条件。防止模型在一次会话里无限调工具。"""

    max_steps: int = 20
    max_tool_calls: int = 15
    timeout_seconds: float = 180.0


@dataclass
class SessionState:
    """单会话状态。同一会话串行处理，M0 不宣称跨进程幂等或恢复能力。"""

    user_id: str
    limits: SessionLimits = field(default_factory=SessionLimits)
    started_at: float = field(default_factory=time.monotonic)
    steps: int = 0
    tool_calls: int = 0
    # 已获用户确认的指纹集合。确认一次只对一组具体参数有效。
    confirmed_fingerprints: set[str] = field(default_factory=set)
    pending: ProposedCall | None = None

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def stop_reason(self) -> str | None:
        """返回应当停止的原因；None 表示可以继续。"""
        if self.steps >= self.limits.max_steps:
            return f"已达最大步骤数 {self.limits.max_steps}"
        if self.tool_calls >= self.limits.max_tool_calls:
            return f"已达最大工具调用数 {self.limits.max_tool_calls}"
        if self.elapsed() >= self.limits.timeout_seconds:
            return f"已超过会话时限 {self.limits.timeout_seconds:.0f} 秒"
        return None

    def confirm(self, call: ProposedCall) -> None:
        """记录用户对某一组具体参数的确认。"""
        self.confirmed_fingerprints.add(call.fingerprint)
        self.pending = None


class WriteGuard:
    """对每一次工具调用做裁决。不依赖模型自觉。"""

    def __init__(self, known_tools: set[str], owner_lookup: Any) -> None:
        """owner_lookup(order_id) -> user_id | None，由 RetailDomain 提供。"""
        self._known_tools = known_tools
        self._owner_lookup = owner_lookup

    def review(self, call: ProposedCall, state: SessionState) -> GuardVerdict:
        if call.name not in self._known_tools:
            return GuardVerdict(
                Decision.DENIED_UNKNOWN_TOOL,
                f"{call.name} 不在本领域的工具清单中，拒绝执行。",
                call,
            )
        if call.name not in WRITE_TOOLS:
            return GuardVerdict(Decision.ALLOW, call=call)

        ownership = self._check_ownership(call, state)
        if ownership is not None:
            return ownership

        if call.fingerprint in state.confirmed_fingerprints:
            return GuardVerdict(Decision.ALLOW, call=call)

        return GuardVerdict(
            Decision.NEEDS_CONFIRMATION,
            f"这是一次会改变订单状态的操作，需要你明确确认：\n{call.describe()}",
            call,
        )

    def _check_ownership(self, call: ProposedCall, state: SessionState) -> GuardVerdict | None:
        """写操作只能作用于当前会话身份名下的资源。返回 None 表示通过。"""
        for field_name in USER_ID_FIELDS:
            target = call.arguments.get(field_name)
            if target is not None and str(target) != state.user_id:
                return GuardVerdict(
                    Decision.DENIED_OWNERSHIP,
                    f"该操作指向用户 {target}，与当前会话身份 {state.user_id} 不符，拒绝执行。",
                    call,
                )

        for field_name in ORDER_ID_FIELDS:
            order_id = call.arguments.get(field_name)
            if order_id is None:
                continue
            owner = self._owner_lookup(str(order_id))
            if owner is None:
                return GuardVerdict(
                    Decision.DENIED_OWNERSHIP,
                    f"订单 {order_id} 不存在或无法确认归属，拒绝执行写操作。",
                    call,
                )
            if owner != state.user_id:
                return GuardVerdict(
                    Decision.DENIED_OWNERSHIP,
                    f"订单 {order_id} 属于其他用户，当前会话身份 {state.user_id} 无权修改。",
                    call,
                )
        return None
