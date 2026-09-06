"""评测栈冻结纪律的测试。不导入 tau2，CI 无需安装 bench 组即可运行。"""

from __future__ import annotations

import pytest

from retail_support_agent.config import CANDIDATE_MODELS, REFERENCE_MODEL, Settings
from retail_support_agent.evaluation import (
    FROZEN_NL_JUDGE,
    FROZEN_USER_SIMULATOR,
    EvalStack,
    EvaluationSetupError,
    litellm_model,
)

DUMMY_KEY = "sk-eval-test-key-000000000000"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
    return Settings()  # type: ignore[call-arg]


def test_frozen_stack_is_outside_the_candidate_pool() -> None:
    """评判与模拟用户若取自候选池，等于让候选模型给自己打分。"""
    assert FROZEN_USER_SIMULATOR not in CANDIDATE_MODELS
    assert FROZEN_NL_JUDGE not in CANDIDATE_MODELS


def test_frozen_stack_uses_dated_snapshots_not_aliases() -> None:
    """别名可能被服务端悄悄更新，导致不同时间的成绩不可比。"""
    for model in (FROZEN_USER_SIMULATOR, FROZEN_NL_JUDGE):
        assert any(char.isdigit() for char in model.split("-")[-1]), f"{model} 看起来不是快照 ID"


def test_candidate_user_simulator_is_rejected(settings: Settings) -> None:
    with pytest.raises(EvaluationSetupError, match="模拟用户"):
        EvalStack(settings=settings, agent_model=REFERENCE_MODEL, user_model=REFERENCE_MODEL)


def test_candidate_judge_is_rejected(settings: Settings) -> None:
    with pytest.raises(EvaluationSetupError, match="评判"):
        EvalStack(settings=settings, agent_model=REFERENCE_MODEL, judge_model="glm-5.2")


def test_default_stack_is_accepted(settings: Settings) -> None:
    stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
    assert stack.user_model == FROZEN_USER_SIMULATOR
    assert stack.judge_model == FROZEN_NL_JUDGE


def test_litellm_prefix_routes_to_openai_compatible_endpoint() -> None:
    assert litellm_model("glm-5.2") == "openai/glm-5.2"


class TestSecretsStayOutOfReports:
    def test_describe_never_contains_the_key(self, settings: Settings) -> None:
        described = EvalStack(settings=settings, agent_model=REFERENCE_MODEL).describe()
        assert DUMMY_KEY not in str(described)

    def test_routing_args_carry_the_key_but_are_not_part_of_describe(
        self, settings: Settings
    ) -> None:
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        # 密钥只能出现在真正发请求的参数里
        assert stack.frozen_args()["api_key"] == DUMMY_KEY
        assert "api_key" not in stack.describe()


def test_frozen_stack_temperature_is_zero(settings: Settings) -> None:
    stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
    assert stack.frozen_args()["temperature"] == 0.0
