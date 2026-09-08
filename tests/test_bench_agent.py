"""护栏接入基准后的裁决逻辑。

这些用例不联网、不计费，也不需要 tau2 的消息类——只测纯逻辑部分
（身份建立、归属学习、三种裁决、确认消费）。Agent 类本身需要 tau2，
单独用一条 importorskip 的用例确认它能构造出来。

**为什么值得单独测**：接入基准后身份不再是给定的，而是从对话里学来的。
学错一次的后果是放行一次本该拦下的写操作——一个不会报错的错误。
"""

from __future__ import annotations

import json

import pytest

from retail_support_agent.bench_agent import (
    AGENT_NAME,
    NO_IDENTITY_REASON,
    UNKNOWN_OWNER_REASON,
    GuardState,
    is_affirmative,
    observe_tool_result,
    review_call,
)
from retail_support_agent.guardrails import (
    Decision,
    ProposedCall,
    SessionState,
    WriteGuard,
)

KNOWN = {
    "get_order_details",
    "find_user_id_by_email",
    "cancel_pending_order",
    "return_delivered_order_items",
}


def has_han(text: str) -> bool:
    """文案里有没有汉字。用码点范围判，不引第三方库。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def order_payload(order_id: str, user_id: str) -> str:
    return json.dumps({"order_id": order_id, "user_id": user_id, "status": "pending"})


class TestIdentityIsLearnedFromTheConversation:
    def test_identity_starts_empty(self) -> None:
        assert GuardState().identity is None

    def test_identity_tool_result_establishes_it(self) -> None:
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        assert guard.identity == "ava_moore_2033"

    def test_an_error_sentence_is_not_taken_as_an_identity(self) -> None:
        """查不到用户时上游返回的是一段说明文字，不能把它当成用户 id。

        当成 id 的后果：归属校验拿一句话去比对，永远不相符——看起来"很安全"，
        实际上是把所有写操作都拦死，而原因完全不可解释。
        """
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "Error: user not found")
        assert guard.identity is None

    def test_writes_are_denied_before_identity_is_established(self) -> None:
        """身份没建立就动写操作，一律拒绝——这是上游政策本来的要求。"""
        guard = GuardState()
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.DENIED_OWNERSHIP
        assert "has not been authenticated" in verdict.reason


class TestOwnershipIsLearnedNotGuessed:
    def test_unknown_order_is_denied_with_an_actionable_reason(self) -> None:
        """没查过归属就拒绝，并告诉模型该先做什么。

        换成"放行"就等于凭订单号猜归属；换成一句无信息的拒绝，
        模型只会原样重试，白烧几轮。
        """
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.DENIED_OWNERSHIP
        assert "get_order_details" in verdict.reason

    def test_known_order_of_another_user_is_denied(self) -> None:
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        observe_tool_result(guard, "get_order_details", order_payload("#W1", "someone_else_1"))
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_malformed_order_payload_teaches_nothing(self) -> None:
        """解析不了就当没学到，绝不猜——猜错会放行本该拦下的写操作。"""
        guard = GuardState()
        observe_tool_result(guard, "get_order_details", "not json at all")
        assert guard.order_owner == {}


class TestConfirmation:
    def _ready(self) -> GuardState:
        guard = GuardState()
        observe_tool_result(guard, "find_user_id_by_email", "ava_moore_2033")
        observe_tool_result(guard, "get_order_details", order_payload("#W1", "ava_moore_2033"))
        return guard

    def test_own_order_still_needs_confirmation(self) -> None:
        """归属没问题也不能直接写——这正是本项目的核心主张。"""
        guard = self._ready()
        verdict = review_call(
            guard, KNOWN, ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        )
        assert verdict.decision is Decision.NEEDS_CONFIRMATION
        # 确认文案必须逐项列出真实参数，不能只问一句"确认吗"
        assert "#W1" in verdict.reason

    def test_confirmed_fingerprint_is_allowed(self) -> None:
        guard = self._ready()
        call = ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        guard.confirmed.add(call.fingerprint)
        assert review_call(guard, KNOWN, call).allowed

    def test_confirmation_does_not_carry_to_different_arguments(self) -> None:
        """确认绑定的是**那一组具体参数**。改一个字段旧确认就失效。

        这条是护栏的关键：模型在拿到确认后改参数（换订单、多加一项），
        必须重新确认，否则"用户同意过"会被拿去为一个没人同意过的操作背书。
        """
        guard = self._ready()
        confirmed = ProposedCall("cancel_pending_order", {"order_id": "#W1"})
        guard.confirmed.add(confirmed.fingerprint)

        observe_tool_result(guard, "get_order_details", order_payload("#W2", "ava_moore_2033"))
        other = ProposedCall("cancel_pending_order", {"order_id": "#W2"})
        assert review_call(guard, KNOWN, other).decision is Decision.NEEDS_CONFIRMATION

        extra = ProposedCall("cancel_pending_order", {"order_id": "#W1", "reason": "changed"})
        assert review_call(guard, KNOWN, extra).decision is Decision.NEEDS_CONFIRMATION

    def test_reads_pass_without_identity(self) -> None:
        """读操作不该被身份要求卡住，否则 Agent 连身份都查不了。"""
        guard = GuardState()
        assert review_call(
            guard, KNOWN, ProposedCall("get_order_details", {"order_id": "#W1"})
        ).allowed


class TestAffirmative:
    @pytest.mark.parametrize(
        "text",
        [
            "yes",
            "Yes, please go ahead",
            "confirm",
            "That's correct",
            "确认执行",
            # dev 实跑抓到的真实回复：引述的订单理由里含 "no longer"
            'Confirmed — cancel #W5199551, "no longer needed."',
            "OK, cancel it. Reason: no longer needed",
        ],
    )
    def test_positive(self, text: str) -> None:
        assert is_affirmative(text)

    @pytest.mark.parametrize(
        "text",
        [
            "no",
            "No, don't cancel that",
            "wait, let me check",
            "not yet",
            "Please do not proceed",
            "别取消",
            "",
        ],
    )
    def test_negative(self, text: str) -> None:
        """含否定词一律不算同意。宁可多问一轮，也不能把「别取消」当成确认。"""
        assert not is_affirmative(text)

    def test_quoted_reason_containing_no_is_not_a_refusal(self) -> None:
        """这条是被一次真实死循环逼出来的，单独立一条以免被当成普通边界用例删掉。

        模拟用户回复 `Confirmed — cancel #W5199551, "no longer needed."`——
        一句毫不含糊的同意。原先用子串匹配 "no"，把它判成拒绝：
        确认永远拿不到，模型重发、护栏再拦，**同一步循环 93 次**直到撞上
        步骤上限，整题记 0 分。分数掉了，而日志里看不出任何"错误"。
        """
        assert is_affirmative('Confirmed — cancel #W5199551, "no longer needed."')
        # 而真正的拒绝仍然要判成拒绝
        assert not is_affirmative("No — that order is no longer needed, leave it")

    def test_unrelated_dont_does_not_veto_an_explicit_yes(self) -> None:
        """同样是被一次真实丢分逼出来的（全集任务 80），与上一条是同一类错误。

        模拟用户看到中文确认提示，回了：

            "my Spanish is decent but I don't think that's Spanish!
             ... If so, then YES — go ahead!"

        那个 "don't" 说的是**语言**，不是操作。原先只要整句里出现 "don't"
        就一票否决，于是同一条消息里毫不含糊的 "YES — go ahead!" 被作废，
        交换始终没有执行，整题 0 分。

        现在否定必须**指向动作**才算数。方向仍然从严：误判成拒绝只多一轮
        往返，误判成同意就是一次未经确认的写入。
        """
        assert is_affirmative(
            "my Spanish is decent but I don't think that's Spanish! If so, then YES — go ahead!"
        )
        # 指向动作的否定仍然一票否决，哪怕同一句里有 "yes"
        assert not is_affirmative("Yes, but don't cancel the order")
        assert not is_affirmative("Please do not proceed with that")


class TestConfirmationPromptLanguage:
    """确认文案必须是英文。

    τ²-bench 的对话全程是英文。中文提示让模拟用户先去猜"这是什么语言"，
    再自己推测被问的是什么——任务 80 就是这样丢的分。文案是产品的一部分，
    不该假设读它的人懂中文。
    """

    def test_describe_is_english(self) -> None:
        text = ProposedCall("cancel_pending_order", {"order_id": "#W001"}).describe()
        assert "Run cancel_pending_order" in text
        assert "#W001" in text
        assert not has_han(text)

    def test_confirmation_reason_is_english(self) -> None:
        guard = WriteGuard(KNOWN, lambda _: "alice")
        verdict = guard.review(
            ProposedCall("cancel_pending_order", {"order_id": "#W001"}),
            SessionState(user_id="alice"),
        )
        assert verdict.decision is Decision.NEEDS_CONFIRMATION
        assert not has_han(verdict.reason)

    def test_denial_reasons_are_english(self) -> None:
        assert not has_han(NO_IDENTITY_REASON)
        assert not has_han(UNKNOWN_OWNER_REASON)


class TestRegistration:
    def test_agent_can_be_built_and_registered(self) -> None:
        """构造得出来才谈得上跑。这条会在上游改了 agent 接口时变红。"""
        pytest.importorskip("tau2", reason="tau2 属 bench 组，未装则跳过")
        from tau2.registry import registry

        from retail_support_agent.bench_agent import build_agent_class, register

        assert build_agent_class() is not None
        assert register() == AGENT_NAME
        assert registry.get_agent_factory(AGENT_NAME) is not None
        # 重复注册要幂等：上游对同名注册直接抛错
        assert register() == AGENT_NAME


class TestBlockedTurnStillReportsItsCost:
    """扣下工具调用不退款：被替换的那条消息必须原样带着 cost/usage。

    上游 `get_cost()` 的规则很狠——整条轨迹里只要有**一条** assistant 消息
    `cost` 为 None，**整轮的 agent_cost 直接变成 None**。于是每一道护栏介入过的题
    都会把成本记成 0，而 M0.5 建立的「与 litellm 独立记账交叉校验」当场失效。
    表现只是成本显示为 0，不报任何错——冒烟第一轮就踩了这个。
    """

    def _agent(self, monkeypatch: pytest.MonkeyPatch, reply: object) -> object:
        pytest.importorskip("tau2", reason="tau2 属 bench 组，未装则跳过")
        import tau2.agent.llm_agent as llm_agent_module

        from retail_support_agent.bench_agent import build_agent_class

        monkeypatch.setattr(llm_agent_module, "generate", lambda **_: reply)

        class FakeTool:
            def __init__(self, name: str) -> None:
                self.name = name

        agent_class = build_agent_class()
        return agent_class(
            tools=[FakeTool(name) for name in KNOWN],
            domain_policy="（测试用政策）",
            llm="openai/fake",
            llm_args={},
        )

    def test_held_message_keeps_cost_and_drops_tool_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage

        wanted = AssistantMessage.text(
            "",
            tool_calls=[
                ToolCall(id="c1", name="cancel_pending_order", arguments={"order_id": "#W1"})
            ],
            cost=0.5,
            usage={"prompt_tokens": 10, "completion_tokens": 2},
        )
        agent = self._agent(monkeypatch, wanted)

        state = agent.get_init_state()  # type: ignore[attr-defined]
        observe_tool_result(state.guard, "find_user_id_by_email", "ava_moore_2033")
        observe_tool_result(
            state.guard, "get_order_details", order_payload("#W1", "ava_moore_2033")
        )

        held, _ = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="cancel it"), state
        )

        assert not held.tool_calls, "被拦下的工具调用不能被发出去"
        assert held.cost == 0.5, "模型那次调用真的花了钱，成本不能丢"
        assert held.usage == {"prompt_tokens": 10, "completion_tokens": 2}
        assert "needs your explicit confirmation" in (held.content or "")

    def test_allowed_call_passes_through_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读操作原样放行，连消息对象都不该被换掉。"""
        from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage

        wanted = AssistantMessage.text(
            "",
            tool_calls=[ToolCall(id="c1", name="get_order_details", arguments={"order_id": "#W1"})],
            cost=0.1,
        )
        agent = self._agent(monkeypatch, wanted)
        state = agent.get_init_state()  # type: ignore[attr-defined]
        out, _ = agent.generate_next_message(  # type: ignore[attr-defined]
            UserMessage(role="user", content="what's my order?"), state
        )
        assert out is wanted
        assert out.tool_calls
