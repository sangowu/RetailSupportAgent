"""评测栈冻结纪律的测试。不导入 tau2，CI 无需安装 bench 组即可运行。"""

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
    """不读 .env：否则开发机上真实的判官配置会渗进单测，结果因人而异。"""
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
    """评判与模拟用户若取自候选池，等于让候选模型给自己打分。"""
    assert FROZEN_USER_SIMULATOR not in CANDIDATE_MODELS
    assert FROZEN_NL_JUDGE not in CANDIDATE_MODELS


def test_frozen_judge_uses_a_dated_snapshot_not_an_alias() -> None:
    """别名可能被服务端悄悄更新，导致不同时间的成绩不可比。"""
    assert any(char.isdigit() for char in FROZEN_NL_JUDGE.split("-")[-1])


def test_frozen_user_simulator_alias_risk_is_recorded() -> None:
    """模拟用户是这条纪律的**已知例外**，把它钉成明账而不是默默破例。

    智谱没有为 glm-5.3-flash 提供带日期的快照 ID（2026-09-07 查阅官方文档）。
    代价是：智谱若在这个别名后面换模型，我们只能靠复跑发现，不会有任何提示。
    这条测试的作用是——哪天有了快照 ID，它会提醒我们去钉上。
    """
    assert FROZEN_USER_SIMULATOR == "glm-5.3-flash"
    assert not any(char.isdigit() for char in FROZEN_USER_SIMULATOR.split("-")[-1])


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
    assert stack.judge_is_independent is False


def test_litellm_prefix_routes_to_openai_compatible_endpoint() -> None:
    assert litellm_model("glm-5.2") == "openai/glm-5.2"


class TestIndependentJudgeEndpoint:
    """判官跑在独立服务商上：被测账户额度耗尽不应连带判官一起挂掉。"""

    def test_judge_model_comes_from_env(self, settings_with_judge: Settings) -> None:
        stack = EvalStack(settings=settings_with_judge, agent_model=REFERENCE_MODEL)
        assert stack.judge_model == "some-judge-model"
        assert stack.judge_is_independent is True

    def test_judge_routes_to_its_own_endpoint_and_key(self, settings_with_judge: Settings) -> None:
        stack = EvalStack(settings=settings_with_judge, agent_model=REFERENCE_MODEL)
        assert stack.judge_args()["api_base"] == "https://judge.example.com"
        assert stack.judge_args()["api_key"] == DUMMY_JUDGE_KEY
        # 被测仍走 DashScope、模拟用户走智谱，三条链路不能串
        assert stack.agent_args()["api_key"] == DUMMY_KEY
        assert "bigmodel" in stack.frozen_args()["api_base"]

    def test_judge_temperature_is_frozen(self, settings_with_judge: Settings) -> None:
        stack = EvalStack(settings=settings_with_judge, agent_model=REFERENCE_MODEL)
        assert stack.judge_args()["temperature"] == 0.0

    def test_partial_config_falls_back_instead_of_half_wiring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只配了 base_url 没配 key，宁可退回默认判官，也不能拿错误的 key 去请求。"""
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
    """精确匹配拦不住同家族模型——那也是一种自评偏袒，必须留痕。"""

    def test_family_extraction(self) -> None:
        assert model_family("deepseek-v4-flash") == "deepseek-v4"
        assert model_family("deepseek-v4-pro-0813") == "deepseek-v4"
        assert model_family("qwen3.8-max") == "qwen3.8"
        assert model_family("glm-5.2") == "glm"

    def test_same_family_judge_produces_a_warning(self) -> None:
        warning = family_collision("deepseek-v4-flash", "NL 断言评判模型")
        assert warning is not None
        assert "deepseek-v4-pro-0813" in warning

    def test_unrelated_judge_produces_no_warning(self) -> None:
        assert family_collision(FROZEN_NL_JUDGE, "NL 断言评判模型") is None

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
    """换 GLM 的代价之三：模拟用户与候选池里的 glm-5.2 同属 glm 家族。

    这条警告从此每轮都会出现。留着而不是消音，因为它是真的：
    哪天拿 glm-5.2 当被测，模拟用户就是它的同门，"聊得来"本身就是一种偏袒。
    这条测试保证有人想通过改 family_collision 来图清净时会被拦下。
    """
    assert family_collision(FROZEN_USER_SIMULATOR, "模拟用户") is not None
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
        # 密钥只能出现在真正发请求的参数里
        assert stack.frozen_args()["api_key"] == FAKE_USER_KEY
        assert "api_key" not in stack.describe()
        assert FAKE_USER_KEY not in str(stack.describe())


def test_frozen_stack_drops_temperature_on_zhipu(settings: Settings) -> None:
    """换 GLM 的代价之二：模拟用户的温度不再由我们控制。

    智谱文档写明思考模式下 temperature 被固定为 1.0。发一个 0 过去不会报错，
    只会让 manifest 记下「我们跑在 temperature=0」的假象——所以干脆不发。
    这意味着模拟用户的复现性下降了，是这次换型实打实的代价，不是笔误。
    """
    stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
    assert "temperature" not in stack.frozen_args()


def test_frozen_stack_temperature_still_applies_off_zhipu(settings: Settings) -> None:
    """纪律本身没废：换回吃温度的模拟用户时，仍然钉死 0。"""
    stack = EvalStack(
        settings=settings, agent_model=REFERENCE_MODEL, user_model=PREVIOUS_USER_SIMULATOR
    )
    assert stack.frozen_args()["temperature"] == FROZEN_STACK_TEMPERATURE


class TestAgentEndpointAndTemperature:
    """被测方的两条纪律：端点可独立、温度必须钉死。"""

    def test_agent_temperature_is_pinned_to_upstream_default(self, settings: Settings) -> None:
        """上游用 default_factory 提供 temperature=0.0，只在字段缺省时生效；
        我们每次都传自己的 dict，不显式带上就等于把它替换掉了。"""
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.agent_args()["temperature"] == 0.0

    def test_agent_falls_back_to_the_shared_endpoint(self, settings: Settings) -> None:
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.agent_is_independent is False
        assert stack.agent_args()["api_key"] == DUMMY_KEY

    def test_agent_can_use_its_own_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一模型跨平台价差可达 3 倍，所以被测方要能单独换端点。"""
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
        # 模拟用户绝不能跟着被测一起搬家——它是冻结评测栈的一部分。
        # 它自己在智谱上，与被测的 DeepSeek 端点无关。
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
    """判官给自家模型打分，是比"同候选池家族"更直接的自评偏袒。"""

    def test_same_family_agent_and_judge_is_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_JUDGE_BASE_URL", "https://api.deepseek.com")
        monkeypatch.setenv("RSA_JUDGE_API_KEY", DUMMY_JUDGE_KEY)
        monkeypatch.setenv("RSA_JUDGE_MODEL", "deepseek-v4-flash")
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model="deepseek-v4-pro",
        )
        assert any("给自家模型的输出打分" in w for w in stack.warnings())

    def test_cross_family_agent_and_judge_is_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        monkeypatch.setenv("RSA_JUDGE_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("RSA_JUDGE_API_KEY", DUMMY_JUDGE_KEY)
        monkeypatch.setenv("RSA_JUDGE_MODEL", "minimax-m2.5")
        stack = EvalStack(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            agent_model="deepseek-v4-pro",
        )
        # 不能再断言"没有任何警告"：冻结模拟用户 glm-5.3-flash 与候选池里的
        # glm-5.2 同属 glm 家族，那条提示现在恒在（见
        # test_frozen_user_simulator_collides_with_a_candidate_family）。
        # 这里要断言的是**被测与判官**不同族，所以只查那一条。
        assert not any("给自家模型的输出打分" in w for w in stack.warnings())


class TestUserSimulatorThinking:
    """模拟用户是冻结评测栈：改它会让全部历史成绩失效。

    2026-09-07 换成 glm-5.3-flash 后，这里的口径变了两处，都是**能力下降**，
    必须由测试记着，不能只当成"改个断言"：
      1. GLM-5.3 系列**不允许关思考**，`user_thinking_disabled` 这个对照开关没了；
      2. GLM 思考模式下 temperature 被服务端固定为 1.0，我们发的 0 不生效。
    """

    def test_thinking_stays_on_by_default(self, settings: Settings) -> None:
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.user_thinking_disabled is False
        assert stack.frozen_args()["extra_body"]["thinking"] == {"type": "enabled"}

    def test_effort_is_pinned_not_left_to_the_server(self, settings: Settings) -> None:
        """档位必须显式下发。

        不下发不等于"用某个固定档位"，而是"用服务端当天的默认值"——
        GLM 实测默认 2698 个推理 token，high 只有 151：差了一个数量级。
        服务商改了默认值，考卷难度就跟着变，而我们不会察觉。
        """
        stack = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert stack.frozen_args()["extra_body"]["reasoning_effort"] == THINKING_EFFORT

    def test_zhipu_dialect_puts_both_keys_at_top_level(self, settings: Settings) -> None:
        """智谱把 thinking 和 reasoning_effort 都放顶层，与另外两家都不同。

        发错方言不会报错，只会被静默忽略——那样跑出来的是"服务端默认档位"的成绩。
        """
        assert EvalStack(settings=settings, agent_model=REFERENCE_MODEL).frozen_args()[
            "extra_body"
        ] == {"thinking": {"type": "enabled"}, "reasoning_effort": THINKING_EFFORT}

    def test_disabling_thinking_fails_at_setup_not_mid_run(self, settings: Settings) -> None:
        """换 GLM 的代价之一：关思考的对照实验做不了了，必须当场报错。

        `frozen_args()` 才是真正会抛的地方，但那时已经在跑批中途——钱花了、
        结果没有。所以构造时就拦。
        """
        with pytest.raises(EvaluationSetupError, match="不支持关闭思考"):
            EvalStack(settings=settings, agent_model=REFERENCE_MODEL, user_thinking_disabled=True)

    def test_the_switch_is_recorded_in_the_manifest(self, settings: Settings) -> None:
        """成绩可不可比取决于这个开关，它必须出现在运行 manifest 里。"""
        on = EvalStack(settings=settings, agent_model=REFERENCE_MODEL)
        assert on.describe()["user_thinking_disabled"] is False

    def test_switch_is_still_available_on_the_previous_simulator(self, settings: Settings) -> None:
        """旧模拟用户仍可关思考：复跑历史对照实验的路没被堵死。"""
        stack = EvalStack(
            settings=settings,
            agent_model=REFERENCE_MODEL,
            user_model=PREVIOUS_USER_SIMULATOR,
            user_thinking_disabled=True,
        )
        assert stack.frozen_args()["extra_body"] == {"enable_thinking": False}
        assert stack.judge_thinking_disabled is True  # 判官独立于此开关
