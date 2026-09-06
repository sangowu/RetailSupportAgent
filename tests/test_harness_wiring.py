"""harness 接线：应用侧与评测侧必须指向同一个模型和同一个端点。

这是最容易日后改岔的地方——评测在 DeepSeek 上跑出 0.600，应用却连着 DashScope，
那评测成绩对线上就没有任何解释力。用测试钉死，而不是靠记忆。
"""

from __future__ import annotations

import json

import pytest

from retail_support_agent.config import CANDIDATE_MODELS, SELECTED_MODEL, Settings
from retail_support_agent.evaluation import EvalStack
from retail_support_agent.llm import THINKING_EFFORT, ModelConfig, ThinkingConfig

DASHSCOPE_KEY = "sk-dashscope-0000000000"
DEEPSEEK_KEY = "sk-deepseek-1111111111"
DEEPSEEK_URL = "https://api.deepseek.com"


@pytest.fixture
def deepseek_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DASHSCOPE_API_KEY", DASHSCOPE_KEY)
    monkeypatch.setenv("RSA_AGENT_BASE_URL", DEEPSEEK_URL)
    monkeypatch.setenv("RSA_AGENT_API_KEY", DEEPSEEK_KEY)
    for name in ("RSA_JUDGE_BASE_URL", "RSA_JUDGE_API_KEY", "RSA_JUDGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """纯 DashScope 端点：用来验证另一套方言。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", DASHSCOPE_KEY)
    for name in (
        "RSA_AGENT_BASE_URL",
        "RSA_AGENT_API_KEY",
        "RSA_JUDGE_BASE_URL",
        "RSA_JUDGE_API_KEY",
        "RSA_JUDGE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestThinkingDialectPerProvider:
    """两家服务商的思考参数完全不同名，发错方言不会报错，只会被静默忽略。"""

    def test_dashscope_gets_dashscope_dialect(self, settings: Settings) -> None:
        stack = EvalStack(settings=settings, agent_model=CANDIDATE_MODELS[0])
        assert stack.agent_args()["extra_body"] == {
            "enable_thinking": True,
            "reasoning_effort": THINKING_EFFORT,
        }

    def test_deepseek_gets_nested_thinking_object(self, deepseek_settings: Settings) -> None:
        stack = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro")
        assert stack.agent_args()["extra_body"] == {
            "thinking": {"type": "enabled", "reasoning_effort": THINKING_EFFORT}
        }

    def test_deepseek_thinking_mode_drops_temperature(self, deepseek_settings: Settings) -> None:
        """DeepSeek 思考模式不支持 temperature，且设了不报错也不生效。

        既然服务端会丢弃，就不要发——发了只会让 manifest 记下一个
        「我们跑在 temperature=0」的假象。
        """
        stack = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro")
        assert "temperature" not in stack.agent_args()

    def test_dashscope_still_gets_temperature(self, settings: Settings) -> None:
        """DashScope 上的候选模型确实吃温度，不能因为 DeepSeek 的限制一刀切。"""
        stack = EvalStack(settings=settings, agent_model=CANDIDATE_MODELS[0])
        assert stack.agent_args()["temperature"] == 0.0

    def test_manifest_records_effort_without_leaking_the_key(
        self, deepseek_settings: Settings
    ) -> None:
        described = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro").describe()
        assert described["agent_thinking_effort"] == THINKING_EFFORT
        assert "api_key" not in described["agent_args_sent"]
        assert DEEPSEEK_KEY not in json.dumps(described)


def test_app_defaults_to_the_selected_model() -> None:
    assert ModelConfig().model_id == SELECTED_MODEL


def test_app_pins_temperature_for_reproducibility() -> None:
    """不传温度会落到服务商默认（0.7~1.0），线上表现就不可复现。"""
    assert ModelConfig().temperature == 0.0


def test_app_defaults_to_thinking_high() -> None:
    """DeepSeek 思考默认就是开的、档位默认 high：不传参数不等于不思考。

    此前这里断言的是 `enabled=False`，与真实行为相反——我们从未向 DeepSeek
    下发过任何思考参数，实测 214 次调用里 190 次产生了推理 token。
    现在配置写成显式的 thinking+high，与项目统一口径一致。
    """
    assert ModelConfig().thinking == ThinkingConfig(enabled=True, effort=THINKING_EFFORT)
    assert THINKING_EFFORT == "high"


def test_selected_model_is_distinct_from_the_dashscope_twin() -> None:
    """DashScope 上的同款叫 deepseek-v4-pro-0813，价格是本模型的 3~5 倍。
    名字必须分开，否则会按错的价目表折算成本。"""
    assert SELECTED_MODEL == "deepseek-v4-pro"
    assert SELECTED_MODEL not in CANDIDATE_MODELS
    assert "deepseek-v4-pro-0813" in CANDIDATE_MODELS


class TestAppAndEvalShareTheSameEndpoint:
    def test_both_resolve_to_the_agent_endpoint(self, deepseek_settings: Settings) -> None:
        stack = EvalStack(settings=deepseek_settings, agent_model=SELECTED_MODEL)
        app_base, app_key = deepseek_settings.agent_endpoint()
        assert app_base == DEEPSEEK_URL
        assert stack.agent_args()["api_base"] == app_base
        assert stack.agent_args()["api_key"] == app_key

    def test_user_simulator_does_not_follow_the_agent(self, deepseek_settings: Settings) -> None:
        """模拟用户是冻结评测栈的一部分，绝不能跟着被测搬家。

        2026-09-07 起它自己住在智谱（glm-5.3-flash），所以这里断言的不再是
        「它留在 DashScope」，而是「它不在被测的端点上」——后者才是要守的不变量。
        """
        stack = EvalStack(settings=deepseek_settings, agent_model=SELECTED_MODEL)
        assert stack.frozen_args()["api_base"] != DEEPSEEK_URL
        assert stack.frozen_args()["api_key"] != DEEPSEEK_KEY
        assert "bigmodel" in stack.frozen_args()["api_base"]

    def test_without_agent_endpoint_everything_stays_on_dashscope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DASHSCOPE_KEY)
        for name in ("RSA_AGENT_BASE_URL", "RSA_AGENT_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.agent_endpoint() == (settings.base_url, DASHSCOPE_KEY)
        assert settings.agent_is_dashscope() is True


class TestProviderSpecificParamsStayWithTheirProvider:
    def test_deepseek_endpoint_is_not_dashscope(self, deepseek_settings: Settings) -> None:
        """enable_thinking 是 DashScope 扩展，不该发给 DeepSeek。"""
        assert deepseek_settings.agent_is_dashscope() is False

    def test_thinking_config_still_rejects_mutually_exclusive_params(self) -> None:
        """M0.2 的结论仍然成立：互斥只有部分模型强制，必须由我们保证只传其一。"""
        with pytest.raises(ValueError, match="不能同时设置"):
            ThinkingConfig(enabled=True, effort="low", budget=8192)
