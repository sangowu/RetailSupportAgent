"""Harness wiring: the application and the evaluation must point at the same model and
the same endpoint.

This is the easiest thing to let drift. If evaluation scores 0.600 on DeepSeek while
the application talks to DashScope, the score explains nothing about production. Pinned
by tests rather than by memory.
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
    """A pure DashScope endpoint, used to exercise the other dialect."""
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
    """The two providers name thinking parameters differently, and the wrong dialect is
    silently ignored rather than rejected."""

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
        """DeepSeek thinking mode does not support temperature and neither errors nor
        applies it.

        Since the server discards it, it is not sent: sending it would only record the
        false claim that we run at temperature=0.
        """
        stack = EvalStack(settings=deepseek_settings, agent_model="deepseek-v4-pro")
        assert "temperature" not in stack.agent_args()

    def test_dashscope_still_gets_temperature(self, settings: Settings) -> None:
        """DashScope candidates do honour temperature; DeepSeek's limitation must not
        remove it everywhere."""
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
    """Omitting temperature falls back to the provider default (0.7-1.0), which makes
    production behaviour irreproducible."""
    assert ModelConfig().temperature == 0.0


def test_app_defaults_to_thinking_high() -> None:
    """DeepSeek defaults to thinking on at level high: sending no parameter is not the
    same as no thinking.

    This used to assert `enabled=False`, the opposite of reality: no thinking parameter
    was ever sent to DeepSeek, and 190 of 214 measured calls produced reasoning tokens.
    The configuration now states thinking+high explicitly.
    """
    assert ModelConfig().thinking == ThinkingConfig(enabled=True, effort=THINKING_EFFORT)
    assert THINKING_EFFORT == "high"


def test_selected_model_is_distinct_from_the_dashscope_twin() -> None:
    """The same model on DashScope is called deepseek-v4-pro-0813 and costs 3-5x more.
    The names must stay separate or costs get computed from the wrong price table."""
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
        """The simulated user is part of the frozen stack and must not move with the agent.

        Since 2026-09-07 it lives on Zhipu (glm-5.3-flash), so the assertion is no longer
        "it stays on DashScope" but "it is not on the agent's endpoint", which is the
        actual invariant.
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
        """enable_thinking is a DashScope extension and must not reach DeepSeek."""
        assert deepseek_settings.agent_is_dashscope() is False

    def test_thinking_config_still_rejects_mutually_exclusive_params(self) -> None:
        """The M0.2 finding still holds: only some models enforce the exclusion, so this
        code must guarantee only one is sent."""
        with pytest.raises(ValueError, match="cannot both be set"):
            ThinkingConfig(enabled=True, effort="low", budget=8192)
