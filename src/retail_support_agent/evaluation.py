"""M0.4 evaluation runs: connect the upstream τ²-bench orchestrator and evaluator to
our model endpoints.

Upstream orchestration, simulated user and scoring logic are not rewritten. Two things
happen here:
1. litellm model routing points at OpenAI-compatible endpoints;
2. the NL-assertion judge is switched from upstream's default gpt-4.1 to a fixed model
   we can reach.

Evaluation-stack discipline (AGENTS.md): once chosen, the simulated user and the judge
do not change, and neither may come from the candidate pool, which would let a
candidate grade itself.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from retail_support_agent.config import (
    CANDIDATE_FAMILIES,
    CANDIDATE_MODELS,
    Settings,
    model_family,
)
from retail_support_agent.llm import (
    THINKING_EFFORT,
    ZHIPU_THINKING_ALWAYS_ON,
    ThinkingConfig,
)
from retail_support_agent.pricing import LITELLM_COST_CURRENCY, register_with_litellm

# The frozen evaluation stack. Neither model is in the candidate pool.
#
# The simulated user changed from qwen3.6-plus-2026-04-02 to glm-5.3-flash on
# 2026-09-07. Not because GLM is stronger, but because qwen was ending the test early:
# it would put "please go change it" and `###STOP###` in the same message, leaving the
# agent no turn in which to act. Paired over 24 tasks, 14/24 -> 23/24, McNemar exact
# p=0.0117 (significant); in 9 of the 10 flipped tasks the GLM user took more turns;
# upstream's UserOnlyReviewer found 0 errors on the GLM side and 3 on the qwen side
# (including one critical_hindered: reading "modify all your pending small tshirt" as
# "only change the order that is already small"). It is also about 11x cheaper
# (simulated-user cost 1.0929 -> 0.1018 CNY per 24 tasks).
# See docs/M0.4_EXPERIMENT_PLAN.md §9.8.
#
# Changing the simulated user makes all earlier scores incomparable, so the old id is
# kept as a constant rather than deleted: reproducing a historical result must point
# back at it explicitly.
FROZEN_USER_SIMULATOR = "glm-5.3-flash"
PREVIOUS_USER_SIMULATOR = "qwen3.6-plus-2026-04-02"

# The judge still uses a dated snapshot id: an alias can be repointed server-side,
# which would make historical results incomparable. No equivalent dated snapshot exists
# for the GLM simulated user. That is an accepted, stated cost: if Zhipu repoints
# `glm-5.3-flash`, only a re-run would reveal it.
FROZEN_NL_JUDGE = "qwen3.6-plus-2026-04-02"

# The frozen simulated user exists only on the Zhipu open platform (bigmodel.cn).
# Without RSA_USER_*, `glm-5.3-flash` would be sent to DashScope, which does not serve
# that model; the error would be unclear, and if DashScope ever did serve it, the run
# would silently measure a different model. This therefore fails hard rather than
# falling back.
FROZEN_USER_SIMULATOR_HOST = "bigmodel"

# The evaluation stack runs at temperature=0, matching upstream defaults, to reduce
# scoring variance.
FROZEN_STACK_TEMPERATURE = 0.0

# Temperature for the agent under test, matching upstream DEFAULT_LLM_TEMPERATURE_AGENT.
# Named separately rather than reusing the constant above: the stack temperature
# (simulated user, judge) is this project's discipline, while the agent's temperature
# aligns with the upstream standard. They change for different reasons and one rename
# should not move both.
AGENT_TEMPERATURE = 0.0

# For DeepSeek this temperature has no effect: the documentation states thinking mode
# does not support temperature and that "setting it does not error and does not take
# effect". The constant and the sending logic stay because DashScope candidates do
# honour it, but any claim that "we run at temperature=0" holds only for DashScope.


# JSON schema for the judge's output, matching upstream `evaluate_nl_assertions()`
# field by field: it indexes result["expectedOutcome"] / ["metExpectation"] /
# ["reasoning"] directly, so a missing key is a KeyError that aborts the run just like
# a json.loads failure. Strict mode requires every object property to be required with
# additionalProperties=false.
NL_JUDGE_JSON_SCHEMA: dict[str, Any] = {
    "name": "nl_assertion_results",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "expectedOutcome": {"type": "string"},
                        "reasoning": {"type": "string"},
                        "metExpectation": {"type": "boolean"},
                    },
                    "required": ["expectedOutcome", "reasoning", "metExpectation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    },
}

# DashScope families supporting strict json_schema mode (documentation read
# 2026-09-06: Qwen3.7-Plus/Flash/Max, Qwen3.8-Max/Flash). Everything else falls back to
# json_object, including the fallback judge qwen3.6-plus.
_JSON_SCHEMA_FAMILIES: frozenset[str] = frozenset({"qwen3.7", "qwen3.8"})


def judge_response_format(model_id: str) -> dict[str, Any]:
    """Which structured-output mode the judge should use.

    Upstream calls `json.loads()` on the judge's reply and indexes keys directly, with
    no error handling. Requiring JSON at the protocol level is more reliable than
    asking for it in the prompt.

    Measured (11 tasks x 2 passes, 2026-09-06): without response_format, the same judge
    reached different conclusions on the same assertion across passes (1 flip in 22
    calls); json_object and json_schema both produced 0 flips, and json_schema matched
    the baseline exactly. json_schema is chosen additionally because it guarantees the
    keys exist, while json_object only guarantees valid JSON.
    """
    if model_family(model_id) in _JSON_SCHEMA_FAMILIES:
        return {"type": "json_schema", "json_schema": NL_JUDGE_JSON_SCHEMA}
    # json_object mode requires the word "json" somewhere in the messages or DashScope
    # errors out. Upstream's judge prompt contains "JSON object", which satisfies it.
    return {"type": "json_object"}


class EvaluationSetupError(RuntimeError):
    """The evaluation stack configuration does not meet the freeze requirements."""


def _assert_not_candidate(model_id: str, role: str) -> None:
    if model_id in CANDIDATE_MODELS:
        raise EvaluationSetupError(
            f"{role} uses candidate model {model_id}, which is self-preference. "
            "Use a model outside the candidate pool"
        )


def family_collision(model_id: str, role: str) -> str | None:
    """Same family but a different model: not blocking, returns a warning that must
    appear in the report.

    Blocking would also rule out the hard requirement that a judge exist, since few
    providers are available. But the risk must not vanish: it goes into `describe()`,
    into the run manifest, and into the report.
    """
    family = model_family(model_id)
    if model_id not in CANDIDATE_MODELS and family in CANDIDATE_FAMILIES:
        kin = sorted(m for m in CANDIDATE_MODELS if model_family(m) == family)
        return (
            f"{role} {model_id} is in the same {family} family as candidate "
            f"{', '.join(kin)}: same-origin models may favour their own style, and any "
            "reading of that candidate's score must state this risk"
        )
    return None


def litellm_model(model_id: str) -> str:
    """DashScope and DeepSeek are both OpenAI-compatible, so litellm uses the openai/ prefix."""
    return f"openai/{model_id}"


@dataclass
class EvalStack:
    """The full model configuration of one evaluation run. Frozen once written to the
    run manifest."""

    settings: Settings
    agent_model: str
    # None = use the frozen default and allow RSA_USER_MODEL from .env to override.
    # An explicit value always wins: reproducing a historical result must point back at
    # the old simulated user, and letting an environment variable override an explicit
    # argument would run a different stack than the one asked for.
    user_model: str | None = None
    judge_model: str | None = None
    agent_extra: dict[str, Any] = field(default_factory=dict)
    # enable_thinking is a DashScope extension that DeepSeek's endpoint rejects, so it
    # is sent only when the judge runs on DashScope.
    judge_thinking_disabled: bool = True
    # Thinking switch for the simulated user. Default False, i.e. thinking stays on:
    # the simulated user is part of the frozen stack, and changing it invalidates every
    # historical score, so only a caller running a deliberate comparison may turn it
    # off. Measured: 92% of its output is reasoning tokens.
    user_thinking_disabled: bool = False
    # Thinking level for the agent under test; the project standard is high
    # (llm.THINKING_EFFORT). DeepSeek already defaults to thinking+high, so stating it
    # explicitly changes no behaviour, but it turns "we thought we were running without
    # thinking" into a checkable record.
    agent_thinking_enabled: bool = True
    agent_thinking_effort: str = THINKING_EFFORT
    # M5.3: whether the thinking mode is chosen per turn. Recorded in the manifest
    # because a routed run and a fixed-mode run are different subjects, and a score
    # that does not say which one it came from cannot be compared with anything.
    route_thinking: bool = False
    # Whether to send historical reasoning_content back (DeepSeek documents this is
    # required when the request carries tools). A switch rather than hard-coded: it
    # adds roughly 7600 characters per conversation to the request, and whether that
    # helps or hurts the score must be measured, not assumed from documentation.
    echo_reasoning: bool = True

    def __post_init__(self) -> None:
        # The judge prefers its own endpoint and otherwise falls back to the frozen
        # model on DashScope.
        if self.judge_model is None:
            configured = self.settings.judge_model
            self.judge_model = (
                configured
                if self.settings.has_independent_judge() and configured
                else FROZEN_NL_JUDGE
            )
        if self.user_model is None:
            configured_user = self.settings.user_model
            self.user_model = (
                configured_user
                if self.settings.has_independent_user() and configured_user
                else FROZEN_USER_SIMULATOR
            )
        judge: str = self.judge_model
        _assert_not_candidate(self.user_model, "The simulated user")
        _assert_not_candidate(judge, "The NL-assertion judge")
        self._assert_user_endpoint_reachable()
        self._assert_user_thinking_switch_is_supported()
        if "dashscope" not in str(self._judge_routing()["api_base"]):
            self.judge_thinking_disabled = False

    def _assert_user_endpoint_reachable(self) -> None:
        """The frozen simulated user must reach the platform that actually serves it.

        `_user_routing()` silently falls back to DashScope when RSA_USER_* is unset.
        That fallback is safe for the judge, which lives on DashScope, but not for the
        simulated user, which has moved to Zhipu. Better to state what is missing than
        to hit an unclear error on DashScope.
        """
        if self.user_model != FROZEN_USER_SIMULATOR:
            return  # the caller chose another simulated user and owns that choice
        api_base = str(self._user_routing()["api_base"])
        if FROZEN_USER_SIMULATOR_HOST not in api_base:
            raise ValueError(
                f"The frozen simulated user {FROZEN_USER_SIMULATOR} exists only on the "
                f"Zhipu open platform, but would be sent to {api_base}. "
                "Set RSA_USER_BASE_URL / RSA_USER_API_KEY / RSA_USER_MODEL in .env "
                "(all three or none). "
                "To reproduce results from before the simulated user changed, pass "
                f"user_model={PREVIOUS_USER_SIMULATOR!r} explicitly"
            )

    def _assert_user_thinking_switch_is_supported(self) -> None:
        """`user_thinking_disabled` is impossible on GLM-5.3 and must be reported here.

        Moving to GLM cost a comparison switch: the GLM-5.3 family refuses to disable
        thinking (server 400, code 1210). Without this check the failure would surface
        when `frozen_args()` is called, which is mid-run, after money has been spent
        and with nothing to show.
        """
        if not self.user_thinking_disabled:
            return
        model = str(self.user_model)
        if any(model.startswith(m) for m in ZHIPU_THINKING_ALWAYS_ON):
            raise EvaluationSetupError(
                f"The simulated user {model} cannot disable thinking (the Zhipu GLM-5.3 "
                "family forces it server-side). For a thinking-off comparison, pass a "
                f"simulated user that supports it, for example "
                f"user_model={PREVIOUS_USER_SIMULATOR!r}"
            )

    @property
    def judge_is_independent(self) -> bool:
        """Whether the judge runs on a different provider than the model under test."""
        return (
            self.settings.has_independent_judge() and self.judge_model == self.settings.judge_model
        )

    def warnings(self) -> list[str]:
        """Configuration-level risks. An empty list means no known risk, not no risk."""
        judge = str(self.judge_model)
        found = [
            family_collision(str(self.user_model), "The simulated user"),
            family_collision(judge, "The NL-assertion judge"),
        ]
        # Agent and judge in the same family is the most direct form of
        # self-preference: the judge is grading its own family's output. This does not
        # consult the candidate pool; it compares the two models of this run.
        if model_family(self.agent_model) == model_family(judge):
            found.append(
                f"The NL-assertion judge {judge} is in the same {model_family(judge)} "
                f"family as the agent under test {self.agent_model}: the judge is "
                "grading its own family's output, the most direct form of "
                "self-preference, and this result cannot be used for cross-family "
                "comparison"
            )
        return [w for w in found if w]

    def _routing(self) -> dict[str, Any]:
        return {
            "api_base": self.settings.base_url,
            "api_key": self.settings.dashscope_api_key.get_secret_value(),
        }

    def _user_routing(self) -> dict[str, Any]:
        """The simulated user may run elsewhere; without its own config it falls back
        to DashScope."""
        if not self.user_is_independent:
            return self._routing()
        assert self.settings.user_api_key is not None  # guaranteed by has_independent_user
        return {
            "api_base": self.settings.user_base_url,
            "api_key": self.settings.user_api_key.get_secret_value(),
        }

    @property
    def user_is_independent(self) -> bool:
        """Whether the simulated user runs on a different platform than the model under test."""
        return self.settings.has_independent_user() and self.user_model == self.settings.user_model

    def _judge_routing(self) -> dict[str, Any]:
        if not self.judge_is_independent:
            return self._routing()
        assert self.settings.judge_api_key is not None  # guaranteed by has_independent_judge
        return {
            "api_base": self.settings.judge_base_url,
            "api_key": self.settings.judge_api_key.get_secret_value(),
        }

    def _agent_routing(self) -> dict[str, Any]:
        # Shares Settings.agent_endpoint() with the application: if the two reached
        # different providers, production behaviour and benchmark scores would not be
        # comparable.
        base_url, api_key = self.settings.agent_endpoint()
        return {"api_base": base_url, "api_key": api_key}

    @property
    def agent_is_independent(self) -> bool:
        """Whether the model under test runs on a different provider than the simulated user."""
        return self.settings.has_independent_agent()

    def agent_args(self) -> dict[str, Any]:
        """Call arguments for the agent under test.

        Temperature must be passed explicitly. Upstream `TextRunConfig.llm_args_agent`
        supplies `{"temperature": 0.0}` through `default_factory`, and a default_factory
        only applies when the field is absent; passing our own dict replaces that
        default entirely. The agent under test therefore ran at each provider's default
        temperature (usually 0.7-1.0), which is neither reproducible nor a fair basis
        for cross-model comparison. Found and fixed 2026-09-06.
        """
        routing = self._agent_routing()
        is_dashscope = "dashscope" in str(routing["api_base"])
        thinking = ThinkingConfig(
            enabled=self.agent_thinking_enabled,
            effort=self.agent_thinking_effort if self.agent_thinking_enabled else None,
        )
        args: dict[str, Any] = {**routing}
        # Temperature has no effect in DeepSeek thinking mode; sending it would only
        # make the manifest lie.
        if not thinking.ignores_temperature(not is_dashscope):
            args["temperature"] = AGENT_TEMPERATURE
        args["extra_body"] = (
            thinking.as_extra_body() if is_dashscope else thinking.as_deepseek_extra_body()
        )
        # agent_extra last: explicit caller arguments have the highest precedence.
        args.update(self.agent_extra)
        return args

    def boosted_agent_args(self) -> dict[str, Any]:
        """Call arguments for a turn M5.3 routing decides is worth thinking about.

        Everything except the thinking switch matches `agent_args`, so a re-run differs
        from the first attempt in exactly one parameter. Temperature is dropped because
        DeepSeek's thinking mode rejects the sampling parameters outright.
        """
        routing = self._agent_routing()
        is_dashscope = "dashscope" in str(routing["api_base"])
        thinking = ThinkingConfig(enabled=True, effort=self.agent_thinking_effort)
        args: dict[str, Any] = {**routing}
        if not thinking.ignores_temperature(not is_dashscope):
            args["temperature"] = AGENT_TEMPERATURE
        args["extra_body"] = (
            thinking.as_extra_body() if is_dashscope else thinking.as_deepseek_extra_body()
        )
        args.update(self.agent_extra)
        return args

    def frozen_args(self) -> dict[str, Any]:
        """Call arguments for the simulated user."""
        routing = self._user_routing()
        args: dict[str, Any] = {**routing, "temperature": FROZEN_STACK_TEMPERATURE}
        # Thinking dialect follows the platform: the wrong dialect does not error, it is
        # silently ignored.
        thinking = ThinkingConfig(
            enabled=not self.user_thinking_disabled,
            effort=None if self.user_thinking_disabled else THINKING_EFFORT,
        )
        base = str(routing["api_base"])
        if "dashscope" in base:
            args["extra_body"] = thinking.as_extra_body()
        elif "deepseek" in base:
            args["extra_body"] = thinking.as_deepseek_extra_body()
            # DeepSeek discards temperature in thinking mode, so it is not sent.
            if not self.user_thinking_disabled:
                args.pop("temperature", None)
        elif "bigmodel" in base:
            args["extra_body"] = thinking.as_zhipu_extra_body(str(self.user_model))
            # Zhipu documents that with thinking on, temperature is fixed at 1.0, so
            # the 0 we send would not apply.
            if not self.user_thinking_disabled:
                args.pop("temperature", None)
        # Other platforms: no dialect is guessed, only generic arguments are sent. A
        # wrong guess is silently ignored, which is harder to find than sending nothing.
        return args

    def judge_args(self) -> dict[str, Any]:
        """Call arguments for the judge, which may point at another provider.

        Thinking must be off. Upstream calls `json.loads(content)` on the judge's reply
        with no error handling, and a model in thinking mode mixes reasoning into
        content, so the same input sometimes returns valid JSON and sometimes aborts
        the run. Measured on `qwen3.7-flash-2026-07-15`: 5570 of 6246 output tokens
        were reasoning. The judge needs stable structured output, not reasoning depth.
        """
        args: dict[str, Any] = {
            **self._judge_routing(),
            "temperature": FROZEN_STACK_TEMPERATURE,
            "response_format": judge_response_format(str(self.judge_model)),
        }
        if self.judge_thinking_disabled:
            args["extra_body"] = {"enable_thinking": False}
        return args

    def describe(self) -> dict[str, Any]:
        """Description for the run manifest. Contains no secrets."""
        return {
            "agent_model": self.agent_model,
            "agent_extra": self.agent_extra,
            "user_simulator": self.user_model,
            "user_simulator_independent_endpoint": self.user_is_independent,
            "user_simulator_base_url": self._user_routing()["api_base"],
            "nl_judge": self.judge_model,
            "agent_temperature": AGENT_TEMPERATURE,
            "agent_thinking_enabled": self.agent_thinking_enabled,
            "agent_thinking_effort": self.agent_thinking_effort,
            "agent_thinking_routed": self.route_thinking,
            "echo_reasoning_content": self.echo_reasoning,
            # Allow-list the non-secret fields. Excluding api_key by deny-list would be
            # unsafe: a new upstream secret field would land in the manifest verbatim.
            "agent_args_sent": {
                k: v for k, v in self.agent_args().items() if k in ("temperature", "extra_body")
            },
            "user_simulator_thinking_effort": (
                None if self.user_thinking_disabled else THINKING_EFFORT
            ),
            "agent_independent_endpoint": self.agent_is_independent,
            "agent_base_url": self._agent_routing()["api_base"],
            "nl_judge_independent_endpoint": self.judge_is_independent,
            "nl_judge_base_url": self._judge_routing()["api_base"],
            "nl_judge_thinking_disabled": self.judge_thinking_disabled,
            "nl_judge_response_format": judge_response_format(str(self.judge_model))["type"],
            "frozen_stack_temperature": FROZEN_STACK_TEMPERATURE,
            "user_thinking_disabled": self.user_thinking_disabled,
            "base_url": self.settings.base_url,
            "warnings": self.warnings(),
        }


def prepare_environment(data_dir: str) -> None:
    """Must be called before importing tau2."""
    os.environ["TAU2_DATA_DIR"] = data_dir


def capture_full_usage() -> None:
    """Make upstream keep the cache and reasoning token fields.

    Upstream's `get_response_usage()` keeps only two fields:

    ```python
    return {"completion_tokens": usage.completion_tokens, "prompt_tokens": usage.prompt_tokens}
    ```

    so `prompt_tokens_details.cached_tokens` and
    `completion_tokens_details.reasoning_tokens` are discarded. That matters: in the
    smoke run 72% of agent-side input tokens were cache hits, billed at 20% of the
    input rate, and 89% of the simulated user's output tokens were reasoning tokens,
    billed at the output rate. Excluding either makes the cost model wrong.

    The raw response was always in `message.raw_data`, so historical results can be
    recovered; this patch only makes the `usage` field self-sufficient.
    """
    from tau2.utils import llm_utils

    if getattr(llm_utils.get_response_usage, "_rsa_patched", False):
        return

    original = llm_utils.get_response_usage

    def get_response_usage_with_details(response: Any) -> dict[str, Any] | None:
        base = original(response)
        if base is None:
            return None
        usage: dict[str, Any] = dict(base)
        raw = response.get("usage")
        prompt_details = getattr(raw, "prompt_tokens_details", None)
        completion_details = getattr(raw, "completion_tokens_details", None)
        usage["cached_tokens"] = int(getattr(prompt_details, "cached_tokens", None) or 0)
        usage["reasoning_tokens"] = int(getattr(completion_details, "reasoning_tokens", None) or 0)
        # DeepSeek's own endpoint uses different field names; accept both.
        for field_name in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = getattr(raw, field_name, None)
            if value is not None:
                usage[field_name] = int(value)
        if usage.get("prompt_cache_hit_tokens") and not usage["cached_tokens"]:
            usage["cached_tokens"] = usage["prompt_cache_hit_tokens"]
        return usage

    get_response_usage_with_details._rsa_patched = True  # type: ignore[attr-defined]
    llm_utils.get_response_usage = get_response_usage_with_details


def echo_reasoning_content() -> None:
    """Send `reasoning_content` back to the model.

    DeepSeek documentation (read 2026-09-06): when a request carries `tools`,
    "reasoning_content from previous turns should be returned to the API and will be
    concatenated into the context". The τ²-bench agent carries tools every turn, while
    upstream's `to_litellm_messages()` returns only `content` and `tool_calls`, so
    reasoning is dropped each turn. Nothing errors; the model just forgets what it was
    thinking.

    This is fixable because upstream stores the whole response in
    `AssistantMessage.raw_data`, where `reasoning_content` has been all along, merely
    not translated back into the request.

    Only meaningful on DeepSeek endpoints (DashScope does not require it), but the
    patch is harmless: messages without `reasoning_content` are untouched.
    """
    from tau2.data_model.message import AssistantMessage
    from tau2.utils import llm_utils

    if getattr(llm_utils.to_litellm_messages, "_rsa_patched", False):
        return

    original = llm_utils.to_litellm_messages

    def reasoning_of(message: Any) -> str | None:
        raw = getattr(message, "raw_data", None) or {}
        choices = raw.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("reasoning_content")
        return str(content) if content else None

    def to_litellm_messages_with_reasoning(messages: list[Any]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = original(messages)
        # Upstream converts 1:1 in order, so indices line up. If that ever stops being
        # true, skip entirely: better to send no reasoning than to attach it to
        # someone else's message.
        if len(converted) != len(messages):
            return converted
        for slot, message in zip(converted, messages, strict=True):
            if isinstance(message, AssistantMessage) and slot.get("role") == "assistant":
                reasoning = reasoning_of(message)
                if reasoning:
                    slot["reasoning_content"] = reasoning
        return converted

    to_litellm_messages_with_reasoning._rsa_patched = True  # type: ignore[attr-defined]
    llm_utils.to_litellm_messages = to_litellm_messages_with_reasoning


@dataclass
class JudgeUsage:
    """Token usage of one judge call. Judge calls are not in `messages`, so they are
    invisible unless recorded here."""

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int


class JudgeUsageRecorder:
    """Record NL judge token usage.

    Judge calls do not appear in a simulation's messages and upstream does not include
    them in `agent_cost`. Reports could previously only say "judge cost unknown", and
    unknown is not zero.
    """

    def __init__(self) -> None:
        self.calls: list[JudgeUsage] = []

    @property
    def totals(self) -> JudgeUsage:
        return JudgeUsage(
            prompt_tokens=sum(c.prompt_tokens for c in self.calls),
            completion_tokens=sum(c.completion_tokens for c in self.calls),
            cached_tokens=sum(c.cached_tokens for c in self.calls),
            reasoning_tokens=sum(c.reasoning_tokens for c in self.calls),
        )

    def describe(self) -> dict[str, Any]:
        totals = self.totals
        return {
            "judge_calls": len(self.calls),
            "prompt_tokens": totals.prompt_tokens,
            "completion_tokens": totals.completion_tokens,
            "cached_tokens": totals.cached_tokens,
            "reasoning_tokens": totals.reasoning_tokens,
        }

    def install(self) -> None:
        """Wrap `generate` in the evaluator module and collect usage per call."""
        from tau2.evaluator import evaluator_nl_assertions as nl

        original = nl.generate
        if getattr(original, "_rsa_recorder", None) is not None:
            return

        def generate_and_record(*args: Any, **kwargs: Any) -> Any:
            message = original(*args, **kwargs)
            usage = getattr(message, "usage", None) or {}
            self.calls.append(
                JudgeUsage(
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    cached_tokens=int(usage.get("cached_tokens") or 0),
                    reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
                )
            )
            return message

        generate_and_record._rsa_recorder = self  # type: ignore[attr-defined]
        nl.generate = generate_and_record


def patch_nl_judge(stack: EvalStack) -> JudgeUsageRecorder:
    """Point the NL-assertion judge at a fixed reachable model and start accounting for it.

    Upstream binds the default into the evaluator module's globals at import time, so
    changing tau2.config is too late; the module global must be replaced directly.
    """
    from tau2.evaluator import evaluator_nl_assertions as nl

    capture_full_usage()
    nl.DEFAULT_LLM_NL_ASSERTIONS = litellm_model(str(stack.judge_model))
    nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = stack.judge_args()
    recorder = JudgeUsageRecorder()
    recorder.install()
    return recorder


def widen_user_review_severity() -> None:
    """Fix an upstream contradiction that silently discards a finding.

    `_parse_user_only_review_response()` reads severity as one of
    `{"minor", "critical_helped", "critical_hindered"}` and the prompt asks for exactly
    those, but the data model declares

    ```python
    severity: Optional[Literal["minor", "critical"]]
    ```

    so as soon as the reviewer finds a real critical error, constructing
    `UserOnlyReviewError` raises ValidationError. Upstream catches it and substitutes a
    fake error with turn_idx=-1, severity=None and reasoning "Failed to parse LLM
    response", while `critical_user_error` is derived from severity and is therefore
    always False.

    The metric fails exactly where it matters: `critical_helped` (the simulated user
    helping when it should not, making the test easier) can never be reported, which is
    the thing it exists to catch. Reproduced 2026-09-06: 1 of 24 tasks hit this.

    This widens the field to the values the parser actually produces, keeping the
    helped/hindered distinction.
    """
    from tau2.data_model.simulation import UserOnlyReviewError

    field = UserOnlyReviewError.model_fields.get("severity")
    if field is None or getattr(UserOnlyReviewError, "_rsa_severity_widened", False):
        return
    field.annotation = Literal["minor", "critical_helped", "critical_hindered"] | None
    UserOnlyReviewError.model_rebuild(force=True)
    UserOnlyReviewError._rsa_severity_widened = True


def patch_user_reviewer(routing: dict[str, Any]) -> JudgeUsageRecorder:
    """Inject routing into the simulated-user reviewer and account for its usage.

    Upstream `UserOnlyReviewer.review()` accepts only `review_model`, not llm_args, so
    without an endpoint and key it fails with "Missing credentials". Handled like the
    NL judge: wrap the module's `generate`, add routing, and collect usage, since these
    calls are also absent from `messages`.
    """
    from tau2.evaluator import review_llm_judge_user_only as review

    capture_full_usage()
    widen_user_review_severity()
    recorder = JudgeUsageRecorder()
    original = review.generate
    if getattr(original, "_rsa_reviewer", None) is not None:
        return recorder

    def generate_with_routing(*args: Any, **kwargs: Any) -> Any:
        message = original(*args, **{**routing, **kwargs})
        usage = getattr(message, "usage", None) or {}
        recorder.calls.append(
            JudgeUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(usage.get("cached_tokens") or 0),
                reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            )
        )
        return message

    generate_with_routing._rsa_reviewer = recorder  # type: ignore[attr-defined]
    review.generate = generate_with_routing
    return recorder


# Langfuse has two callback names, and the wrong one fails silently rather than erroring.
#
# Measured 2026-09-07 on langfuse 4.15.1 + litellm 1.82.6:
#   "langfuse"      -> initialization raises AttributeError (module 'langfuse' has no
#                      attribute 'version', a v2 SDK attribute); litellm catches it and
#                      returns None
#   "langfuse_otel" -> returns LangfuseOtelLogger normally
#
# Upstream tau2 uses the former (`llm_utils.py`: `litellm.success_callback =
# ["langfuse"]`), so flipping `tau2.config.USE_LANGFUSE` to True does not work: a run
# completes normally with no trace in the dashboard and no sign anything is wrong.
LANGFUSE_CALLBACK = "langfuse_otel"

# The broken callback name, kept so that a code search for "langfuse" leads here from
# anyone following upstream documentation.
LANGFUSE_BROKEN_CALLBACK = "langfuse"


def enable_langfuse(settings: Settings) -> dict[str, Any]:
    """Enable Langfuse when configured and return status for the manifest (no secrets).

    Without all three settings it stays off: a partial configuration cannot connect and
    would only surface mid-run.

    Cost in the dashboard is 0. That is a known upstream gap, measured 2026-09-07:

      Langfuse receives traces, latency, model name and input/output token counts.
      It does not receive cost: litellm 1.82.6's langfuse_otel integration never sends
      `langfuse.observation.cost_details.*` (the string does not appear in the module).
      It writes cost as `gen_ai.cost.*` attributes, which Langfuse's ingestion does not
      recognize. Measured: `costDetails: {}` and `modelId: null` on the observation.

    Letting Langfuse compute cost from usage does not work either: the `usageDetails`
    it receives has only input/output and no cache breakdown, while 95% of agent-side
    input is cache hits at 1/30 of the full rate. Full-price arithmetic overstates cost
    roughly 5x, and a plausible wrong number is harder to notice than a zero.

    So the cost convention here is: Langfuse for traces, latency and token observation
    only; cost comes from scripts/analyze_eval.py, which cross-checks against litellm's
    accounting and matches it exactly.

    Also: the prices registered with litellm are in CNY (pricing.LITELLM_COST_CURRENCY),
    recorded in summary.json so nobody later reads them as USD.
    """
    if not settings.has_langfuse():
        return {
            "langfuse_enabled": False,
            "langfuse_reason": "LANGFUSE_* is not fully configured",
        }

    assert settings.langfuse_public_key and settings.langfuse_secret_key  # per has_langfuse
    host = str(settings.langfuse_host)
    # litellm's langfuse_otel callback reads configuration from the environment; there
    # is no argument for it. Set in-process only, never written to disk or logged.
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key.get_secret_value()
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key.get_secret_value()
    os.environ["LANGFUSE_HOST"] = host
    os.environ["LANGFUSE_OTEL_HOST"] = host

    import litellm

    callbacks = list(getattr(litellm, "callbacks", []) or [])
    if LANGFUSE_CALLBACK not in callbacks:
        callbacks.append(LANGFUSE_CALLBACK)
    litellm.callbacks = callbacks
    return {
        "langfuse_enabled": True,
        "langfuse_callback": LANGFUSE_CALLBACK,
        "langfuse_host": host,
        # The dashboard prefixes this number with a dollar sign, but it is CNY.
        # Recorded in the manifest so nobody reports the dashboard figure as USD.
        "langfuse_cost_currency": LITELLM_COST_CURRENCY,
        # A dashboard cost of 0 is a known gap, not a free run. Cost comes from
        # analyze_eval.py.
        "langfuse_cost_reported": False,
        "langfuse_cost_note": (
            "litellm 1.82.6's langfuse_otel does not send cost_details, and the usage "
            "Langfuse receives has no cache breakdown, so computing cost there "
            "overstates it about 5x. Cost comes from scripts/analyze_eval.py"
        ),
    }


# A dashboard trace has to map back to the run that produced it, or observation and
# results describe different things. Langfuse groups by session: calls sharing a
# session_id appear as one session. The run directory name is used as the session_id,
# since it is already this project's unique identifier for a run (for example
# 20260907T141354Z_deepseek-v4-pro_dev), so no lookup table is needed.
LANGFUSE_TAG = "retail-support-agent"

# The session id lives in a dict rather than a closure variable: resuming calls
# tag_langfuse_session a second time, when the wrapper is already installed and only
# the id needs to change, without stacking another wrapper.
_LANGFUSE_SESSION: dict[str, str] = {}


def tag_langfuse_session(session_id: str) -> str:
    """Tag every LLM call in this run so dashboard traces map back to the run directory.

    litellm's langfuse_otel only reads `session_id` / `tags` from
    `litellm_params["metadata"]` (see `LangfuseOtelLogger._set_metadata_attributes`),
    and those can only be passed per call: there is no global entry point and no
    environment variable (the only one, `LANGFUSE_TRACING_ENVIRONMENT`, is a dev/prod
    environment name, not a session id). Upstream tau2 offers no way to pass metadata,
    so `tau2.utils.llm_utils.completion` is wrapped instead: it is the single exit
    point shared by all three roles.

    Wrapped here rather than at `litellm.completion` because the agent under test, the
    simulated user and both judges all call through this name, so one wrapper covers
    everything; patching litellm's global function would affect any other use in the
    same process.

    `metadata` is litellm's own parameter (it appears in `get_litellm_params`), so it
    is not forwarded to the model provider and is unaffected by `litellm.drop_params`.

    Tagging is harmless when Langfuse is disabled: metadata simply carries a field
    nobody reads.
    """
    from tau2.utils import llm_utils

    _LANGFUSE_SESSION["id"] = session_id
    original = llm_utils.completion
    if getattr(original, "_rsa_session_tagger", False):
        return session_id

    def completion_with_session(*args: Any, **kwargs: Any) -> Any:
        metadata = dict(kwargs.get("metadata") or {})
        # setdefault: if upstream ever starts passing metadata, theirs wins.
        metadata.setdefault("session_id", _LANGFUSE_SESSION["id"])
        metadata.setdefault("tags", [LANGFUSE_TAG, _LANGFUSE_SESSION["id"]])
        return original(*args, **{**kwargs, "metadata": metadata})

    completion_with_session._rsa_session_tagger = True  # type: ignore[attr-defined]
    llm_utils.completion = completion_with_session
    return session_id


# The background logging thread takes a few seconds to create spans, and the batch
# exporter then still holds some. Three seconds measured as sufficient; waiting three
# seconds is cheaper than losing the traces of a run's last few tasks.
LANGFUSE_FLUSH_WAIT_SECONDS = 3.0
LANGFUSE_FLUSH_TIMEOUT_MS = 15_000


def flush_langfuse(wait_seconds: float = LANGFUSE_FLUSH_WAIT_SECONDS) -> bool:
    """Push buffered traces after a run. Returns whether the flush succeeded.

    Skipping this silently loses traces (measured 2026-09-07): a process that makes one
    call and exits never shows that trace in the dashboard, with exit code 0 and
    nothing in the log. A long evaluation loses only its final tasks, because the batch
    exporter ships earlier spans on schedule - and those final tasks are exactly what
    someone checks the dashboard for.

    OTEL's TracerProvider would shut down at exit on its own, but litellm's
    langfuse_otel never installs the provider globally
    (`opentelemetry.trace.get_tracer_provider()` still returns a ProxyTracerProvider),
    so that exit path never runs. The only handle is the logger instance's own
    `tracer.span_processor`.

    The wait is necessary: litellm's success callback runs on another thread, and
    force_flush before the span exists pushes nothing. A fixed wait is used because no
    more precise signal is available.

    `_in_memory_loggers` is a private upstream name; if it cannot be reached this
    returns False. Failing to export observability should not fail an evaluation, so
    this reports rather than raises.
    """
    try:
        from litellm.litellm_core_utils.litellm_logging import _in_memory_loggers
    except ImportError:
        return False

    processors = [
        processor
        for logger in _in_memory_loggers
        if (processor := getattr(getattr(logger, "tracer", None), "span_processor", None))
        is not None
        and hasattr(processor, "force_flush")
    ]
    if not processors:
        return False

    time.sleep(wait_seconds)
    flushed = True
    for processor in processors:
        try:
            flushed = bool(processor.force_flush(LANGFUSE_FLUSH_TIMEOUT_MS)) and flushed
        except Exception:  # observability failures must not break the run
            flushed = False
    return flushed


def build_run_config(
    stack: EvalStack,
    task_ids: list[str],
    num_trials: int,
    max_concurrency: int,
    seed: int,
    save_to: str | None,
    guarded: bool = False,
    route_thinking: bool = False,
) -> Any:
    """Build the upstream run configuration. Task sets come from the M0.1 frozen lists."""
    # Prices must be registered before any call happens, or upstream's
    # `get_response_cost()` catches the exception and returns 0.0, which is why
    # agent_cost was long stuck at 0. The registered prices are CNY, not USD; see
    # pricing.LITELLM_COST_CURRENCY.
    register_with_litellm()
    # In thinking mode DeepSeek expects historical reasoning_content to be sent back.
    # Upstream does not, so this adds it.
    if stack.echo_reasoning:
        echo_reasoning_content()
    # Subject under test: by default upstream's own agent, which measures "model plus
    # evaluation stack"; with guarded=True, the agent wrapped in this project's guard,
    # which measures this project's implementation. The two are different subjects with
    # separate baselines and their scores are not comparable.
    agent_name = "llm_agent"
    if guarded:
        from retail_support_agent.bench_agent import register

        # M5.3: with routing on, the run's own arguments are the cheap mode and the
        # agent holds a second set for the turns a trigger picks out. force=True because
        # the registry keeps the first registration, and a process that registered the
        # unrouted agent earlier would otherwise run the wrong subject in silence.
        boost = stack.boosted_agent_args() if route_thinking else None
        agent_name = register(force=route_thinking, boost_args=boost)

    from tau2.data_model.simulation import TextRunConfig

    return TextRunConfig(
        domain="retail",
        task_ids=task_ids,
        task_split_name=None,  # tasks are named exactly by task_ids; no second filter
        agent=agent_name,
        llm_agent=litellm_model(stack.agent_model),
        llm_args_agent=stack.agent_args(),
        user="user_simulator",
        llm_user=litellm_model(str(stack.user_model)),
        llm_args_user=stack.frozen_args(),
        num_trials=num_trials,
        max_concurrency=max_concurrency,
        seed=seed,
        save_to=save_to,
        log_level="ERROR",
    )
