"""Tests for evaluation-stack freeze discipline. Does not import tau2, so CI runs them
without the bench group."""

from __future__ import annotations

import pytest

from conftest import FAKE_USER_KEY
from retail_support_agent.config import (
    CANDIDATE_MODELS,
    REFERENCE_MODEL,
    Settings,
    model_family,
)
from retail_support_agent.evaluation import (
    FROZEN_NL_JUDGE,
    FROZEN_STACK_TEMPERATURE,
    FROZEN_USER_SIMULATOR,
    PREVIOUS_USER_SIMULATOR,
    EvalStack,
    EvaluationSetupError,
    family_collision,
    litellm_model,
)
from retail_support_agent.llm import THINKING_EFFORT

DUMMY_KEY = "sk-eval-test-key-000000000000"
DUMMY_JUDGE_KEY = "sk-judge-test-key-111111111111"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Do not read .env, or a developer's real judge configuration would leak into unit
    tests and results would differ per machine."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
    for name in ("RSA_JUDGE_BASE_URL", "RSA_JUDGE_API_KEY", "RSA_JUDGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def settings_with_judge(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("RSA_JUDGE_BASE_URL", "https://judge.example.com")
    monkeypatch.setenv("RSA_JUDGE_API_KEY", DUMMY_JUDGE_KEY)
    monkeypatch.setenv("RSA_JUDGE_MODEL", "some-judge-model")
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_frozen_stack_is_outside_the_candidate_pool() -> None:
    """A judge or simulated user from the candidate pool lets a candidate grade itself."""
    assert FROZEN_USER_SIMULATOR not in CANDIDATE_MODELS
    assert FROZEN_NL_JUDGE not in CANDIDATE_MODELS


def test_frozen_judge_uses_a_dated_snapshot_not_an_alias() -> None:
    """An alias can be repointed server-side, making scores from different dates
    incomparable."""
    assert any(char.isdigit() for char in FROZEN_NL_JUDGE.split("-")[-1])


def test_frozen_user_simulator_alias_risk_is_recorded() -> None:
    """The simulated user is a known exception to that rule, recorded rather than
    quietly broken.

    Zhipu publishes no dated snapshot id for glm-5.3-flash (documentation read
    2026-09-07). The cost: if Zhipu repoints the alias, only a re-run would reveal it.
    This test exists so that the day a snapshot id appears, we pin it.
    """
    assert FROZEN_USER_SIMULATOR == "glm-5.3-flash"
    assert not any(char.isdigit() for char in FROZEN_USER_SIMULATOR.split("-")[-1])


def test_candidate_user_simulator_is_rejected(settings: Settings) -> None:
    with pytest.raises(EvaluationSetupError, match="simulated user"):
        EvalStack(settings=settings, agent_model=REFERENCE_MODEL, user_model=REFERENCE_MODEL)


def test_candidate_judge_is_rejected(settings: Settings) -> None:
    with pytest.raises(EvaluationSetupError, match="judge"):
        EvalStack(settings=settings, agent_model=REFERENCE_MODEL, judge_model="glm-5.2")


def test_default_stack_is_accepted(settings: Settings) -> None:
    stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
    assert stack.user_model == FROZEN_USER_SIMULATOR
    assert stack.judge_model == FROZEN_NL_JUDGE
    assert stack.judge_is_independent is False


def test_litellm_prefix_routes_to_openai_compatible_endpoint() -> None:
    assert litellm_model("glm-5.2") == "openai/glm-5.2"


class TestIndependentJudgeEndpoint:
    """The judge runs on its own provider: exhausting the agent's quota must not take
    the judge down with it."""

    def test_judge_model_comes_from_env(self, settings_with_judge: Settings) -> None:
        stack = EvalStack(settings=settings_with_judge, agent_model=REFERENCE_MODEL)
        assert stack.judge_model == "some-judge-model"
        assert stack.judge_is_independent is True

    def test_judge_routes_to_its_own_endpoint_and_key(self, settings_with_judge: Settings) -> None:
        stack = EvalStack(settings=settings_with_judge, agent_model=REFERENCE_MODEL)
        assert stack.judge_args()["api_base"] == "https://judge.example.com"
        assert stack.judge_args()["api_key"] == DUMMY_JUDGE_KEY
        # The agent stays on DashScope and the simulated user on Zhipu; the three
        # routes must not cross.
        assert stack.agent_args()["api_key"] == DUMMY_KEY
        assert "bigmodel" in stack.frozen_args()["api_base"]

    def test_judge_temperature_is_frozen(self, settings_with_judge: Settings) -> None:
        stack = EvalStack(settings=settings_with_judge, agent_model=REFERENCE_MODEL)
        assert stack.judge_args()["temperature"] == 0.0

    def test_partial_config_falls_back_instead_of_half_wiring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a base_url but no key, fall back to the default judge rather than send
        the wrong key."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_JUDGE_BASE_URL", "https://judge.example.com")
        monkeypatch.delenv("RSA_JUDGE_API_KEY", raising=False)
        monkeypatch.delenv("RSA_JUDGE_MODEL", raising=False)
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model=REFERENCE_MODEL,
        )
        assert stack.judge_model == FROZEN_NL_JUDGE
        assert stack.judge_is_independent is False


class TestFamilyCollision:
    """Exact matching misses same-family models, which is also self-preference and must
    be recorded."""

    def test_family_extraction(self) -> None:
        assert model_family("deepseek-v4-flash") == "deepseek-v4"
        assert model_family("deepseek-v4-pro-0813") == "deepseek-v4"
        assert model_family("qwen3.8-max") == "qwen3.8"
        assert model_family("glm-5.2") == "glm"

    def test_same_family_judge_produces_a_warning(self) -> None:
        warning = family_collision("deepseek-v4-flash", "The NL-assertion judge")
        assert warning is not None
        assert "deepseek-v4-pro-0813" in warning

    def test_unrelated_judge_produces_no_warning(self) -> None:
        assert family_collision(FROZEN_NL_JUDGE, "The NL-assertion judge") is None

    def test_warning_reaches_the_run_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_JUDGE_BASE_URL", "https://api.deepseek.com")
        monkeypatch.setenv("RSA_JUDGE_API_KEY", DUMMY_JUDGE_KEY)
        monkeypatch.setenv("RSA_JUDGE_MODEL", "deepseek-v4-flash")
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model=REFERENCE_MODEL,
        )
        assert any("deepseek-v4" in w for w in stack.describe()["warnings"])


def test_frozen_user_simulator_collides_with_a_candidate_family() -> None:
    """Third cost of moving to GLM: the simulated user shares the glm family with
    candidate glm-5.2.

    That warning now appears on every run. It stays rather than being silenced because
    it is true: with glm-5.2 as the agent under test, the simulated user is its
    relative, and getting along is itself a bias. This test blocks anyone tempted to
    edit family_collision for quiet.
    """
    assert family_collision(FROZEN_USER_SIMULATOR, "The simulated user") is not None
    assert model_family(FROZEN_USER_SIMULATOR) == model_family("glm-5.2")


class TestSecretsStayOutOfReports:
    def test_describe_never_contains_the_key(self, settings: Settings) -> None:
        described = EvalStack(settings=settings, agent_model=REFERENCE_MODEL).describe()
        assert DUMMY_KEY not in str(described)

    def test_describe_never_contains_the_judge_key(self, settings_with_judge: Settings) -> None:
        described = EvalStack(settings=settings_with_judge, agent_model=REFERENCE_MODEL).describe()
        assert DUMMY_JUDGE_KEY not in str(described)
        assert DUMMY_KEY not in str(described)

    def test_routing_args_carry_the_key_but_are_not_part_of_describe(
        self, settings: Settings
    ) -> None:
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        # A key may appear only in the arguments that actually make the request.
        assert stack.frozen_args()["api_key"] == FAKE_USER_KEY
        assert "api_key" not in stack.describe()
        assert FAKE_USER_KEY not in str(stack.describe())


def test_frozen_stack_drops_temperature_on_zhipu(settings: Settings) -> None:
    """Second cost of moving to GLM: the simulated user's temperature is no longer ours
    to set.

    Zhipu documents that thinking mode fixes temperature at 1.0. Sending 0 does not
    error; it would only record the false claim that we run at temperature=0, so it is
    not sent. Reproducibility of the simulated user is lower as a result. That is a
    real cost of the switch, not an oversight.
    """
    stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
    assert "temperature" not in stack.frozen_args()


def test_frozen_stack_temperature_still_applies_off_zhipu(settings: Settings) -> None:
    """The rule itself stands: with a simulated user that honours temperature, it is
    still pinned to 0."""
    stack = EvalStack(
        settings=settings, agent_model=REFERENCE_MODEL, user_model=PREVIOUS_USER_SIMULATOR
    )
    assert stack.frozen_args()["temperature"] == FROZEN_STACK_TEMPERATURE


class TestAgentEndpointAndTemperature:
    """Two rules for the agent under test: its endpoint may be separate, its temperature
    must be pinned."""

    def test_agent_temperature_is_pinned_to_upstream_default(self, settings: Settings) -> None:
        """Upstream supplies temperature=0.0 through default_factory, which applies only
        when the field is absent; passing our own dict without it replaces the default."""
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.agent_args()["temperature"] == 0.0

    def test_agent_falls_back_to_the_shared_endpoint(self, settings: Settings) -> None:
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.agent_is_independent is False
        assert stack.agent_args()["api_key"] == DUMMY_KEY

    def test_agent_can_use_its_own_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same model can cost 3x more on another platform, so the agent must be
        able to move endpoints on its own."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_AGENT_BASE_URL", "https://api.deepseek.com")
        monkeypatch.setenv("RSA_AGENT_API_KEY", "sk-agent-test-key-2222")
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model="deepseek-v4-pro",
        )
        assert stack.agent_is_independent is True
        assert stack.agent_args()["api_base"] == "https://api.deepseek.com"
        assert stack.agent_args()["api_key"] == "sk-agent-test-key-2222"
        # The simulated user must not follow the agent: it is part of the frozen stack
        # and lives on Zhipu, unrelated to the agent's DeepSeek endpoint.
        assert "bigmodel" in stack.frozen_args()["api_base"]
        assert stack.frozen_args()["api_key"] != "sk-agent-test-key-2222"

    def test_half_configured_agent_endpoint_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_AGENT_BASE_URL", "https://api.deepseek.com")
        monkeypatch.delenv("RSA_AGENT_API_KEY", raising=False)
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model=REFERENCE_MODEL,
        )
        assert stack.agent_is_independent is False


class TestAgentJudgeFamilyCollision:
    """A judge grading its own family is more direct self-preference than sharing a
    candidate-pool family."""

    def test_same_family_agent_and_judge_is_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_JUDGE_BASE_URL", "https://api.deepseek.com")
        monkeypatch.setenv("RSA_JUDGE_API_KEY", DUMMY_JUDGE_KEY)
        monkeypatch.setenv("RSA_JUDGE_MODEL", "deepseek-v4-flash")
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model="deepseek-v4-pro",
        )
        assert any("grading its own family" in w for w in stack.warnings())

    def test_cross_family_agent_and_judge_is_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_JUDGE_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("RSA_JUDGE_API_KEY", DUMMY_JUDGE_KEY)
        monkeypatch.setenv("RSA_JUDGE_MODEL", "minimax-m2.5")
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model="deepseek-v4-pro",
        )
        # "No warnings at all" is no longer assertable: the frozen simulated user
        # glm-5.3-flash shares the glm family with candidate glm-5.2, so that warning is
        # permanent (see test_frozen_user_simulator_collides_with_a_candidate_family).
        # This asserts only that agent and judge are in different families.
        assert not any("grading its own family" in w for w in stack.warnings())


class TestUserSimulatorThinking:
    """The simulated user is part of the frozen stack: changing it invalidates every
    historical score.

    Two capabilities were lost when it moved to glm-5.3-flash on 2026-09-07, and the
    tests record both rather than treating them as assertion edits:
      1. the GLM-5.3 family cannot disable thinking, so the `user_thinking_disabled`
         comparison switch is gone;
      2. GLM fixes temperature at 1.0 in thinking mode, so the 0 we send does not apply.
    """

    def test_thinking_stays_on_by_default(self, settings: Settings) -> None:
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.user_thinking_disabled is False
        assert stack.frozen_args()["extra_body"]["thinking"] == {"type": "enabled"}

    def test_effort_is_pinned_not_left_to_the_server(self, settings: Settings) -> None:
        """The level must be sent explicitly.

        Omitting it does not mean "some fixed level", it means "whatever the server
        defaults to that day": GLM's default measured 2698 reasoning tokens against 151
        at high, an order of magnitude apart. If the provider changes the default, the
        difficulty of the test changes and we would not notice.
        """
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.frozen_args()["extra_body"]["reasoning_effort"] == THINKING_EFFORT

    def test_zhipu_dialect_puts_both_keys_at_top_level(self, settings: Settings) -> None:
        """Zhipu puts thinking and reasoning_effort at the top level, unlike the other two.

        The wrong dialect is silently ignored, and the run would then measure the
        server's default level.
        """
        assert EvalStack(settings=settings, agent_model=REFERENCE_MODEL).frozen_args()[
            "extra_body"
        ] == {"thinking": {"type": "enabled"}, "reasoning_effort": THINKING_EFFORT}

    def test_disabling_thinking_fails_at_setup_not_mid_run(self, settings: Settings) -> None:
        """First cost of moving to GLM: the thinking-off comparison is impossible and
        must fail immediately.

        `frozen_args()` is where it would actually raise, but that is mid-batch, after
        money has been spent and with nothing to show. So it is caught at construction.
        """
        with pytest.raises(EvaluationSetupError, match="cannot disable thinking"):
            EvalStack(settings=settings, agent_model=REFERENCE_MODEL, user_thinking_disabled=True)

    def test_the_switch_is_recorded_in_the_manifest(self, settings: Settings) -> None:
        """Comparability depends on this switch, so it must appear in the run manifest."""
        on = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert on.describe()["user_thinking_disabled"] is False

    def test_switch_is_still_available_on_the_previous_simulator(self, settings: Settings) -> None:
        """The old simulated user can still disable thinking, so historical comparisons
        remain reproducible."""
        stack = EvalStack(
            settings=settings,
            agent_model=REFERENCE_MODEL,
            user_model=PREVIOUS_USER_SIMULATOR,
            user_thinking_disabled=True,
        )
        assert stack.frozen_args()["extra_body"] == {"enable_thinking": False}
        assert stack.judge_thinking_disabled is True  # the judge is independent of it
