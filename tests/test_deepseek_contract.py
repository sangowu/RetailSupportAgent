"""把 DeepSeek 官方文档（2026-09-06 查阅）的硬约束钉成测试。

为什么值得单独一个文件：这四条约束**都不会报错**。参数发错方言、
温度被丢弃、思考默认开着、推理内容没回传——服务端一律照单全收，
只是不按你以为的方式工作。唯一能防住的手段就是把文档写成断言。

来源：
- https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
- https://api-docs.deepseek.com/zh-cn/api/create-chat-completion
- https://api-docs.deepseek.com/zh-cn/guides/json_mode
- https://api-docs.deepseek.com/zh-cn/guides/tool_calls
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from retail_support_agent.agent import _assistant_message
from retail_support_agent.config import Settings
from retail_support_agent.evaluation import (
    FROZEN_NL_JUDGE,
    NL_JUDGE_JSON_SCHEMA,
    PREVIOUS_USER_SIMULATOR,
    EvalStack,
    judge_response_format,
)
from retail_support_agent.llm import (
    DASHSCOPE_EFFORTS,
    DEEPSEEK_EFFORTS,
    THINKING_EFFORT,
    ThinkingConfig,
)

DEEPSEEK_KEY = "sk-deepseek-contract-2222222222"


@pytest.fixture
def deepseek_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-contract-333333")
    monkeypatch.setenv("RSA_AGENT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("RSA_AGENT_API_KEY", DEEPSEEK_KEY)
    for name in ("RSA_JUDGE_BASE_URL", "RSA_JUDGE_API_KEY", "RSA_JUDGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestThinkingConfigSpeaksBothDialects:
    def test_deepseek_nests_effort_inside_thinking(self) -> None:
        """DeepSeek 的档位是 `thinking.reasoning_effort`，不是顶层参数。"""
        body = ThinkingConfig(enabled=True, effort="high").as_deepseek_extra_body()
        assert body == {"thinking": {"type": "enabled", "reasoning_effort": "high"}}

    def test_deepseek_disable_is_explicit(self) -> None:
        """DeepSeek 思考**默认开启**，不发参数不等于关掉——只能显式 disabled。"""
        assert ThinkingConfig(enabled=False).as_deepseek_extra_body() == {
            "thinking": {"type": "disabled"}
        }

    def test_dashscope_keeps_flat_dialect(self) -> None:
        body = ThinkingConfig(enabled=True, effort="high").as_extra_body()
        assert body == {"enable_thinking": True, "reasoning_effort": "high"}

    def test_thinking_budget_is_rejected_on_deepseek(self) -> None:
        """DeepSeek 没有 thinking_budget。静默忽略会让「已限制思考长度」变成错觉。"""
        with pytest.raises(ValueError, match="thinking_budget"):
            ThinkingConfig(enabled=True, effort=None, budget=8192).as_deepseek_extra_body()


class TestEffortValues:
    def test_project_pins_high(self) -> None:
        assert THINKING_EFFORT == "high"

    def test_high_is_legal_on_both_providers(self) -> None:
        """项目统一用 high，前提是两家都认它。"""
        assert THINKING_EFFORT in DEEPSEEK_EFFORTS
        assert THINKING_EFFORT in DASHSCOPE_EFFORTS

    def test_max_is_deepseek_only(self) -> None:
        """2026-09-06 实测：`max` 发给 DashScope 会 400，整轮评测中断。

        错误原文：'reasoning_effort' must be one of:
        'none', 'minimal', 'low', 'medium', 'high', 'xhigh'
        此前 VALID_EFFORTS 用的是 DeepSeek 那套并全局套用，
        `--thinking max` 打到 DashScope 端点会把整轮跑崩。
        """
        assert "max" in DEEPSEEK_EFFORTS
        assert "max" not in DASHSCOPE_EFFORTS
        with pytest.raises(ValueError, match="DashScope 不接受"):
            ThinkingConfig(effort="max").as_extra_body()

    def test_xhigh_is_dashscope_only(self) -> None:
        assert "xhigh" in DASHSCOPE_EFFORTS
        assert "xhigh" not in DEEPSEEK_EFFORTS
        with pytest.raises(ValueError, match="DeepSeek 不接受"):
            ThinkingConfig(effort="xhigh").as_deepseek_extra_body()

    def test_nonsense_is_rejected_before_it_reaches_the_wire(self) -> None:
        for rejected in ("banana", "HIGH", ""):
            with pytest.raises(ValueError, match="reasoning_effort"):
                ThinkingConfig(effort=rejected)

    def test_effort_and_budget_remain_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="不能同时设置"):
            ThinkingConfig(effort="high", budget=8192)


class TestTemperatureIsIgnoredInThinkingMode:
    """「思考模式不支持 temperature，设置不会报错但也不会生效。」"""

    def test_deepseek_thinking_ignores_temperature(self) -> None:
        assert ThinkingConfig(enabled=True).ignores_temperature(is_deepseek=True) is True

    def test_deepseek_without_thinking_still_uses_temperature(self) -> None:
        assert ThinkingConfig(enabled=False).ignores_temperature(is_deepseek=True) is False

    def test_dashscope_always_uses_temperature(self) -> None:
        assert ThinkingConfig(enabled=True).ignores_temperature(is_deepseek=False) is False


class TestReasoningContentIsEchoedBack:
    """「若请求携带 tools 参数：历史轮次的 reasoning_content 均应回传给 API。」

    我们每一轮都带 tools。丢掉它不报错，模型只是每轮忘记自己刚才的推理。
    """

    def _message(self, reasoning: Any) -> SimpleNamespace:
        return SimpleNamespace(content="好的", reasoning_content=reasoning, tool_calls=None)

    def test_reasoning_is_carried_into_the_next_request(self) -> None:
        payload = _assistant_message(self._message("先查订单状态，再决定能否取消"), [])
        assert payload["reasoning_content"] == "先查订单状态，再决定能否取消"

    def test_absent_reasoning_adds_no_field(self) -> None:
        """未携带 tools 的请求里回传也会被忽略；没有就别造一个空字段出来。"""
        assert "reasoning_content" not in _assistant_message(self._message(None), [])
        assert "reasoning_content" not in _assistant_message(self._message(""), [])

    def test_tool_calls_survive_alongside_reasoning(self) -> None:
        call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="get_order", arguments='{"order_id": "W1"}'),
        )
        payload = _assistant_message(self._message("需要先查单"), [call])
        assert payload["reasoning_content"] == "需要先查单"
        assert payload["tool_calls"][0]["function"]["name"] == "get_order"


class TestJudgeStructuredOutput:
    """判官的结构化输出按模型能力分档。

    实测（11 题 × 2 遍，2026-09-06）：不设 response_format 时同一判官会自相矛盾；
    json_object 与 json_schema 都稳定，后者还能保证键存在。
    """

    def test_qwen37_and_qwen38_get_strict_schema(self) -> None:
        for model in ("qwen3.7-flash-2026-07-15", "qwen3.8-max", "qwen3.8-flash"):
            fmt = judge_response_format(model)
            assert fmt["type"] == "json_schema"
            assert fmt["json_schema"]["strict"] is True

    def test_older_models_fall_back_to_json_object(self) -> None:
        """兜底判官 qwen3.6-plus 不在 json_schema 支持名单里，强上会直接报错。"""
        assert judge_response_format(FROZEN_NL_JUDGE)["type"] == "json_object"
        assert judge_response_format("glm-5.2")["type"] == "json_object"

    def test_schema_matches_what_upstream_indexes(self) -> None:
        """上游按键取值，少一个键就是 KeyError——和解析失败一样会跑崩整轮。"""
        item = NL_JUDGE_JSON_SCHEMA["schema"]["properties"]["results"]["items"]
        assert set(item["required"]) == {"expectedOutcome", "reasoning", "metExpectation"}
        assert item["additionalProperties"] is False
        assert NL_JUDGE_JSON_SCHEMA["schema"]["required"] == ["results"]


class TestReasoningEchoIsAblatable:
    """回传推理内容每段对话多塞约 7600 字符，是正是负必须能实测。

    文档要求回传，但"文档要求"不等于"对我们这批题有好处"——
    做成开关，才能用消融实验回答，而不是靠权威。
    """

    def test_echo_is_on_by_default(self, deepseek_settings: Settings) -> None:
        stack = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro")
        assert stack.echo_reasoning is True

    def test_the_switch_is_recorded_in_the_manifest(self, deepseek_settings: Settings) -> None:
        """成绩可不可比取决于这个开关，它必须出现在运行 manifest 里。"""
        off = EvalStack(
            settings=deepseek_settings, agent_model="deepseek-v4-pro", echo_reasoning=False
        )
        assert off.describe()["echo_reasoning_content"] is False


class TestZhipuDialect:
    """智谱是第三套方言：`thinking` 与 `reasoning_effort` 都在顶层，不嵌套。

    国内版 bigmodel.cn 与境外版 z.ai 的端点和价格都不同，不能混用。
    """

    def test_thinking_and_effort_are_both_top_level(self) -> None:
        assert ThinkingConfig(effort="high").as_zhipu_extra_body("glm-5.3-flash") == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }

    def test_glm_53_refuses_to_disable_thinking(self) -> None:
        """GLM-5.3 系列传 disabled 会被服务端拒绝，本地就该拦下来。"""
        with pytest.raises(ValueError, match="不允许关闭思考"):
            ThinkingConfig(enabled=False).as_zhipu_extra_body("glm-5.3-flash")

    def test_effort_set_differs_from_the_other_two(self) -> None:
        with pytest.raises(ValueError, match="智谱"):
            ThinkingConfig(effort="medium").as_zhipu_extra_body("glm-5.3-flash")


class TestUserSimulatorCanMovePlatform:
    def test_missing_config_fails_loudly_instead_of_falling_back(
        self, monkeypatch: pytest.MonkeyPatch, deepseek_settings: Settings
    ) -> None:
        """没配 RSA_USER_* 时**必须报错**，不能静默退回 DashScope。

        换成 glm-5.3-flash 之后，静默退回的后果变了：DashScope 上没有这个型号，
        运行会在跑批中途炸掉；万一哪天它有了，跑出来的就是另一个模型的成绩，
        而 manifest 里仍写着 glm-5.3-flash。两种结局都比当场报错糟。
        """
        for name in ("RSA_USER_BASE_URL", "RSA_USER_API_KEY", "RSA_USER_MODEL"):
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="只在智谱开放平台"):
            EvalStack(settings=settings, agent_model="deepseek-v4-pro")

    def test_configured_endpoint_is_used(self, deepseek_settings: Settings) -> None:
        stack = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro")
        assert stack.user_is_independent is True
        assert "bigmodel" in stack.frozen_args()["api_base"]

    def test_previous_simulator_still_routes_to_dashscope(
        self, deepseek_settings: Settings
    ) -> None:
        """复跑换型前的历史结论：显式指回旧模拟用户，它仍走 DashScope。"""
        stack = EvalStack(
            settings=deepseek_settings,
            agent_model="deepseek-v4-pro",
            user_model=PREVIOUS_USER_SIMULATOR,
        )
        assert "dashscope" in stack.frozen_args()["api_base"]

    def test_half_configured_falls_back_rather_than_pretending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只配一半会静默退回 DashScope——那样跑出的成绩会被误以为是新平台的。"""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-contract-333333")
        monkeypatch.setenv("RSA_USER_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        monkeypatch.delenv("RSA_USER_API_KEY", raising=False)
        monkeypatch.delenv("RSA_USER_MODEL", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.has_independent_user() is False


class TestUpstreamSeverityContradiction:
    """上游提示词要 critical_helped/critical_hindered，数据模型只收 minor/critical。

    后果不是报错，而是**静默吞掉结论**：审出严重错误 -> 构造失败 -> 被兜成
    一条 severity=None 的伪错误 -> critical_user_error 恒为 False。
    实测复现后，同一批轨迹从「0 处错误」变成「1 处 critical_helped」。
    """

    def test_widened_severity_accepts_what_the_parser_produces(self) -> None:
        pytest.importorskip("tau2")
        from retail_support_agent.evaluation import widen_user_review_severity

        widen_user_review_severity()
        from tau2.data_model.simulation import UserOnlyReviewError

        for value in ("minor", "critical_helped", "critical_hindered"):
            assert UserOnlyReviewError(severity=value, reasoning="x").severity == value

    def test_still_rejects_values_the_parser_never_produces(self) -> None:
        pytest.importorskip("tau2")
        from pydantic import ValidationError

        from retail_support_agent.evaluation import widen_user_review_severity

        widen_user_review_severity()
        from tau2.data_model.simulation import UserOnlyReviewError

        with pytest.raises(ValidationError):
            UserOnlyReviewError(severity="banana", reasoning="x")
