"""Pin the hard constraints from DeepSeek's documentation (read 2026-09-06) as tests.

These deserve their own file because none of the four constraints raises an error. The
wrong dialect, a discarded temperature, thinking on by default, reasoning not sent
back: the server accepts all of it and simply does not behave as assumed. Turning the
documentation into assertions is the only defence.

Sources:
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
        """On DeepSeek the level is `thinking.reasoning_effort`, not a top-level field."""
        body = ThinkingConfig(enabled=True, effort="high").as_deepseek_extra_body()
        assert body == {"thinking": {"type": "enabled", "reasoning_effort": "high"}}

    def test_deepseek_disable_is_explicit(self) -> None:
        """DeepSeek thinking is on by default; sending nothing does not turn it off."""
        assert ThinkingConfig(enabled=False).as_deepseek_extra_body() == {
            "thinking": {"type": "disabled"}
        }

    def test_dashscope_keeps_flat_dialect(self) -> None:
        body = ThinkingConfig(enabled=True, effort="high").as_extra_body()
        assert body == {"enable_thinking": True, "reasoning_effort": "high"}

    def test_thinking_budget_is_rejected_on_deepseek(self) -> None:
        """DeepSeek has no thinking_budget. Ignoring it silently would turn "thinking
        length is capped" into a false belief."""
        with pytest.raises(ValueError, match="thinking_budget"):
            ThinkingConfig(enabled=True, effort=None, budget=8192).as_deepseek_extra_body()


class TestEffortValues:
    def test_project_pins_high(self) -> None:
        assert THINKING_EFFORT == "high"

    def test_high_is_legal_on_both_providers(self) -> None:
        """The project standardizes on high, which requires both providers to accept it."""
        assert THINKING_EFFORT in DEEPSEEK_EFFORTS
        assert THINKING_EFFORT in DASHSCOPE_EFFORTS

    def test_max_is_deepseek_only(self) -> None:
        """Measured 2026-09-06: `max` sent to DashScope returns 400 and aborts the run.

        Error text: 'reasoning_effort' must be one of:
        'none', 'minimal', 'low', 'medium', 'high', 'xhigh'
        VALID_EFFORTS previously used DeepSeek's set globally, so `--thinking max`
        against a DashScope endpoint would break the whole run.
        """
        assert "max" in DEEPSEEK_EFFORTS
        assert "max" not in DASHSCOPE_EFFORTS
        with pytest.raises(ValueError, match="DashScope rejects"):
            ThinkingConfig(effort="max").as_extra_body()

    def test_xhigh_is_dashscope_only(self) -> None:
        assert "xhigh" in DASHSCOPE_EFFORTS
        assert "xhigh" not in DEEPSEEK_EFFORTS
        with pytest.raises(ValueError, match="DeepSeek rejects"):
            ThinkingConfig(effort="xhigh").as_deepseek_extra_body()

    def test_nonsense_is_rejected_before_it_reaches_the_wire(self) -> None:
        for rejected in ("banana", "HIGH", ""):
            with pytest.raises(ValueError, match="reasoning_effort"):
                ThinkingConfig(effort=rejected)

    def test_effort_and_budget_remain_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="cannot both be set"):
            ThinkingConfig(effort="high", budget=8192)


class TestTemperatureIsIgnoredInThinkingMode:
    """ "Thinking mode does not support temperature; setting it does not error and does
    not take effect.\""""

    def test_deepseek_thinking_ignores_temperature(self) -> None:
        assert ThinkingConfig(enabled=True).ignores_temperature(is_deepseek=True) is True

    def test_deepseek_without_thinking_still_uses_temperature(self) -> None:
        assert ThinkingConfig(enabled=False).ignores_temperature(is_deepseek=True) is False

    def test_dashscope_always_uses_temperature(self) -> None:
        assert ThinkingConfig(enabled=True).ignores_temperature(is_deepseek=False) is False


class TestReasoningContentIsEchoedBack:
    """ "When the request carries tools, reasoning_content from previous turns should be
    returned to the API."

    Every turn here carries tools. Dropping it raises nothing; the model just forgets
    its own reasoning each turn.
    """

    def _message(self, reasoning: Any) -> SimpleNamespace:
        return SimpleNamespace(content="ok", reasoning_content=reasoning, tool_calls=None)

    def test_reasoning_is_carried_into_the_next_request(self) -> None:
        payload = _assistant_message(self._message("check the order, then decide"), [])
        assert payload["reasoning_content"] == "check the order, then decide"

    def test_absent_reasoning_adds_no_field(self) -> None:
        """Requests without tools ignore it anyway; do not invent an empty field."""
        assert "reasoning_content" not in _assistant_message(self._message(None), [])
        assert "reasoning_content" not in _assistant_message(self._message(""), [])

    def test_tool_calls_survive_alongside_reasoning(self) -> None:
        call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="get_order", arguments='{"order_id": "W1"}'),
        )
        payload = _assistant_message(self._message("look the order up first"), [call])
        assert payload["reasoning_content"] == "look the order up first"
        assert payload["tool_calls"][0]["function"]["name"] == "get_order"


class TestJudgeStructuredOutput:
    """Structured output for the judge depends on what the model supports.

    Measured (11 tasks x 2 passes, 2026-09-06): without response_format the same judge
    contradicts itself; json_object and json_schema are both stable, and the latter
    also guarantees the keys exist.
    """

    def test_qwen37_and_qwen38_get_strict_schema(self) -> None:
        for model in ("qwen3.7-flash-2026-07-15", "qwen3.8-max", "qwen3.8-flash"):
            fmt = judge_response_format(model)
            assert fmt["type"] == "json_schema"
            assert fmt["json_schema"]["strict"] is True

    def test_older_models_fall_back_to_json_object(self) -> None:
        """The fallback judge qwen3.6-plus does not support json_schema; forcing it errors."""
        assert judge_response_format(FROZEN_NL_JUDGE)["type"] == "json_object"
        assert judge_response_format("glm-5.2")["type"] == "json_object"

    def test_schema_matches_what_upstream_indexes(self) -> None:
        """Upstream indexes keys directly, so a missing key is a KeyError that aborts
        the run just like a parse failure."""
        item = NL_JUDGE_JSON_SCHEMA["schema"]["properties"]["results"]["items"]
        assert set(item["required"]) == {"expectedOutcome", "reasoning", "metExpectation"}
        assert item["additionalProperties"] is False
        assert NL_JUDGE_JSON_SCHEMA["schema"]["required"] == ["results"]


class TestReasoningEchoIsAblatable:
    """Echoing reasoning adds roughly 7600 characters per conversation, and whether that
    helps must be measurable.

    The documentation asks for it, but "the documentation says so" is not "it helps on
    these tasks". A switch allows an ablation instead of an appeal to authority.
    """

    def test_echo_is_on_by_default(self, deepseek_settings: Settings) -> None:
        stack = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro")
        assert stack.echo_reasoning is True

    def test_the_switch_is_recorded_in_the_manifest(self, deepseek_settings: Settings) -> None:
        """Comparability depends on this switch, so it must appear in the run manifest."""
        off = EvalStack(
            settings=deepseek_settings, agent_model="deepseek-v4-pro", echo_reasoning=False
        )
        assert off.describe()["echo_reasoning_content"] is False


class TestZhipuDialect:
    """Zhipu is a third dialect: `thinking` and `reasoning_effort` are both top level.

    bigmodel.cn and z.ai differ in both endpoints and pricing and must not be mixed.
    """

    def test_thinking_and_effort_are_both_top_level(self) -> None:
        assert ThinkingConfig(effort="high").as_zhipu_extra_body("glm-5.3-flash") == {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }

    def test_glm_53_refuses_to_disable_thinking(self) -> None:
        """The GLM-5.3 family rejects disabled server-side, so it is caught locally."""
        with pytest.raises(ValueError, match="cannot disable thinking"):
            ThinkingConfig(enabled=False).as_zhipu_extra_body("glm-5.3-flash")

    def test_effort_set_differs_from_the_other_two(self) -> None:
        with pytest.raises(ValueError, match="Zhipu"):
            ThinkingConfig(effort="medium").as_zhipu_extra_body("glm-5.3-flash")


class TestUserSimulatorCanMovePlatform:
    def test_missing_config_fails_loudly_instead_of_falling_back(
        self, monkeypatch: pytest.MonkeyPatch, deepseek_settings: Settings
    ) -> None:
        """Without RSA_USER_* this must fail rather than fall back to DashScope silently.

        After the move to glm-5.3-flash the consequence of a silent fallback changed:
        DashScope does not serve that model, so the run would break mid-batch; and if it
        ever did serve it, the run would measure a different model while the manifest
        still said glm-5.3-flash. Both outcomes are worse than failing immediately.
        """
        for name in ("RSA_USER_BASE_URL", "RSA_USER_API_KEY", "RSA_USER_MODEL"):
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="Zhipu open platform"):
            EvalStack(settings=settings, agent_model="deepseek-v4-pro")

    def test_configured_endpoint_is_used(self, deepseek_settings: Settings) -> None:
        stack = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro")
        assert stack.user_is_independent is True
        assert "bigmodel" in stack.frozen_args()["api_base"]

    def test_previous_simulator_still_routes_to_dashscope(
        self, deepseek_settings: Settings
    ) -> None:
        """Reproducing pre-switch results: naming the old simulated user explicitly
        still routes to DashScope."""
        stack = EvalStack(
            settings=deepseek_settings,
            agent_model="deepseek-v4-pro",
            user_model=PREVIOUS_USER_SIMULATOR,
        )
        assert "dashscope" in stack.frozen_args()["api_base"]

    def test_half_configured_falls_back_rather_than_pretending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-set configuration falls back to DashScope, and the resulting scores
        would be read as belonging to the new platform."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-contract-333333")
        monkeypatch.setenv("RSA_USER_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        monkeypatch.delenv("RSA_USER_API_KEY", raising=False)
        monkeypatch.delenv("RSA_USER_MODEL", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.has_independent_user() is False


class TestUpstreamSeverityContradiction:
    """Upstream's prompt asks for critical_helped/critical_hindered while the data model
    accepts only minor/critical.

    The consequence is not an error but a silently discarded finding: a critical error
    is found, construction fails, upstream substitutes a fake error with severity=None,
    and critical_user_error is therefore always False. After reproducing it, the same
    traces went from "0 errors" to "1 critical_helped".
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
