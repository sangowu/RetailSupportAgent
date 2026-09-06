"""业务闭环的端到端测试：假模型 + 假领域，不联网、不计费。

重点验证"模型说了不算"——即使模型直接提议写操作、或声称已获确认，
仍必须先拿到用户针对具体参数的确认才会真正执行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from retail_support_agent.agent import RetailAgent, _is_affirmative
from retail_support_agent.domain import ToolResult
from retail_support_agent.guardrails import SessionLimits, SessionState

# ---- 测试替身 -----------------------------------------------------------


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
    """按剧本依次返回预设响应，并记录收到的消息，便于断言工具结果确实回传了。"""

    def __init__(self, script: list[FakeCompletion]) -> None:
        self.script = list(script)
        self.seen_messages: list[list[dict[str, Any]]] = []

    def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self.seen_messages.append([dict(m) for m in messages])
        return self.script.pop(0) if self.script else reply("（剧本已用尽）")


class FakeDomain:
    """替代 RetailDomain，避免测试依赖上游数据。接口保持一致。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.owners = {"#W001": "alice", "#W002": "bob"}

    @property
    def policy(self) -> str:
        return "（测试用政策）"

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


# ---- 测试 ---------------------------------------------------------------


class TestReadPath:
    def test_read_tool_executes_and_result_returns_to_model(self) -> None:
        agent, domain = build(
            [
                tool_call("get_order_details", {"order_id": "#W001"}),
                reply("你的订单 #W001 状态是 pending。"),
            ]
        )
        result = agent.send("查一下 #W001")
        assert result.status == "answered"
        assert domain.executed == [("get_order_details", {"order_id": "#W001"})]
        assert "pending" in result.reply


class TestWriteRequiresConfirmation:
    def test_write_is_held_until_user_confirms(self) -> None:
        agent, domain = build([tool_call("cancel_pending_order", {"order_id": "#W001"})])
        result = agent.send("帮我取消 #W001")

        assert result.status == "needs_confirmation"
        assert domain.executed == [], "未确认前绝不能执行写操作"
        assert result.pending_confirmation is not None
        assert "#W001" in result.pending_confirmation

    def test_write_executes_after_explicit_confirmation(self) -> None:
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W001"}),
                tool_call("cancel_pending_order", {"order_id": "#W001"}, call_id="c2"),
                reply("订单 #W001 已取消。"),
            ]
        )
        agent.send("帮我取消 #W001")
        result = agent.send("确认")

        assert result.status == "answered"
        assert domain.executed == [("cancel_pending_order", {"order_id": "#W001"})]

    def test_declining_keeps_the_write_unexecuted(self) -> None:
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W001"}),
                reply("好的，已取消该操作。"),
            ]
        )
        agent.send("帮我取消 #W001")
        agent.send("不用了")
        assert domain.executed == []

    def test_confirmation_does_not_transfer_to_different_arguments(self) -> None:
        """确认的是 #W001；模型改成 #W002 后必须重新确认（且会被归属拦下）。"""
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W001"}),
                tool_call("cancel_pending_order", {"order_id": "#W002"}, call_id="c2"),
                reply("已按你的要求处理。"),
            ]
        )
        agent.send("帮我取消 #W001")
        agent.send("确认")
        assert domain.executed == [], "确认只对被确认的那组参数有效"


class TestOwnershipInFlow:
    def test_other_users_order_is_refused_with_reason(self) -> None:
        agent, domain = build(
            [
                tool_call("cancel_pending_order", {"order_id": "#W002"}),
                reply("抱歉，该订单不属于你。"),
            ]
        )
        result = agent.send("取消 #W002")
        assert domain.executed == []
        assert result.status in {"denied", "answered"}


class TestMalformedToolCall:
    def test_invalid_json_arguments_do_not_crash(self) -> None:
        broken = FakeCompletion(
            [FakeChoice(FakeMessage(None, [FakeToolCall("c1", FakeFunction("x", "{not json"))]))]
        )
        agent, domain = build([broken, reply("我需要更准确的信息。")])
        result = agent.send("随便")
        assert domain.executed == []
        assert result.status == "answered"


class TestStopConditions:
    def test_step_limit_ends_the_turn(self) -> None:
        agent, _ = build([reply("hi")], max_steps=0)
        result = agent.send("你好")
        assert result.status == "stopped"
        assert result.stop_reason is not None


class TestAffirmativeDetection:
    @pytest.mark.parametrize("text", ["确认", "是的", "可以", "OK", "好的，确认执行"])
    def test_affirmative(self, text: str) -> None:
        assert _is_affirmative(text)

    @pytest.mark.parametrize("text", ["不用了", "先别", "no", "cancel", "我不确认"])
    def test_negative(self, text: str) -> None:
        assert not _is_affirmative(text)
