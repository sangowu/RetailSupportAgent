"""M0.4 评测运行：把上游 τ²-bench 的编排器与评分器接到我们的模型端点上。

不重写上游的编排、模拟用户与评分逻辑，只做两件事：
1. 把 litellm 的模型路由指向 DashScope 的 OpenAI 兼容端点；
2. 把 NL 断言评判模型从上游默认的 gpt-4.1 换成我们能访问的**固定**模型。

评测栈的冻结纪律（AGENTS.md）：模拟用户与评判模型一旦选定就不再变动，
且**不从候选池里选**——否则等于让候选模型给自己打分。
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

# 冻结的评测栈。二者都不在候选池内。
#
# 模拟用户于 2026-09-07 从 qwen3.6-plus-2026-04-02 换成 glm-5.3-flash。
# 换的理由不是「GLM 更强」，是 **qwen 在提前掐断考试**：实测它会把
# 「请你去改」和 `###STOP###` 写进同一条消息，Agent 根本没有回合执行动作。
# 24 题配对对比 14/24 -> 23/24，McNemar 精确检验 p=0.0117（显著），
# 10 道翻盘题里有 9 道 GLM 的用户轮数更多；上游 UserOnlyReviewer 在 GLM 侧
# 找到 0 个错误，qwen 侧 3 个（含 1 个 critical_hindered：把
# 「modify all your pending small tshirt」读成「只改已经是 small 的那单」）。
# 顺带便宜约 11 倍（模拟用户成本 1.0929 -> 0.1018 元/24 题）。
# 详见 docs/M0.4_EXPERIMENT_PLAN.md §9.8。
#
# **换模拟用户会让此前全部成绩不可比**，所以旧型号保留成常量而不是删掉：
# 复跑历史结论时必须显式指回它。
FROZEN_USER_SIMULATOR = "glm-5.3-flash"
PREVIOUS_USER_SIMULATOR = "qwen3.6-plus-2026-04-02"

# 判官仍用带日期的快照 ID：别名会被服务端在背后换模型，历史结果就不可比了。
# 模拟用户换成 GLM 后没有等价的日期快照可用——这是一笔明账，
# 代价是智谱若在 `glm-5.3-flash` 后面换模型，我们只能靠复跑发现。
FROZEN_NL_JUDGE = "qwen3.6-plus-2026-04-02"

# 冻结的模拟用户只在智谱开放平台（国内版 bigmodel.cn）上有。
# 没配 RSA_USER_* 就会把 `glm-5.3-flash` 发去 DashScope——那边不认这个型号，
# 报错信息未必直白，且一旦哪天认了，跑出来的就是另一个模型的成绩。
# 所以这里**硬失败**，不做静默退回。
FROZEN_USER_SIMULATOR_HOST = "bigmodel"

# 评测栈一律 temperature=0，与上游默认一致，减少评分本身的波动。
FROZEN_STACK_TEMPERATURE = 0.0

# 被测 Agent 的温度，与上游 DEFAULT_LLM_TEMPERATURE_AGENT 一致。
# 单独命名而不是复用上面那个常量：评测栈（模拟用户/判官）的温度是我们的纪律，
# 被测方的温度是**对齐上游标准**，两者改动的理由不同，不该被一次改名同时动到。
AGENT_TEMPERATURE = 0.0

# 对 DeepSeek 而言这个温度**不会生效**：官方文档写明思考模式不支持 temperature，
# 且「设置参数不会报错，但也不会生效」。仍然保留常量与下发逻辑，因为 DashScope
# 上的候选模型确实吃它——但任何"我们跑在 temperature=0"的说法，只对 DashScope 成立。


# 判官输出的 JSON Schema，与上游 `evaluate_nl_assertions()` 的解析代码逐字段对应：
# 它直接取 result["expectedOutcome"] / ["metExpectation"] / ["reasoning"]，
# 少一个键就是 KeyError，和 json.loads 失败一样会把整轮跑崩。
# strict 模式要求每个 object 的属性全部 required 且 additionalProperties=false。
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

# 支持 json_schema 严格模式的 DashScope 家族（文档 2026-09-06 查阅：
# Qwen3.7-Plus/Flash/Max、Qwen3.8-Max/Flash 系列）。其余模型退回 json_object——
# 包括我们的兜底判官 qwen3.6-plus，它属于「仅支持 json_object」的一档。
_JSON_SCHEMA_FAMILIES: frozenset[str] = frozenset({"qwen3.7", "qwen3.8"})


def judge_response_format(model_id: str) -> dict[str, Any]:
    """判官该用哪种结构化输出。

    上游拿到判官回复直接 `json.loads()` 再按键取值，全程零容错。
    与其在提示词里求它输出 JSON，不如在协议层要求。

    实测（11 题 × 2 遍，2026-09-06）：不设 response_format 时，同一判官对同一条
    断言两遍给出不同结论（22 次调用里 1 条翻转）；json_object 与 json_schema
    都是 0 条翻转，且 json_schema 的判定与基线完全一致。
    选 json_schema 的额外理由是它能保证键存在，json_object 只保证是合法 JSON。
    """
    if model_family(model_id) in _JSON_SCHEMA_FAMILIES:
        return {"type": "json_schema", "json_schema": NL_JUDGE_JSON_SCHEMA}
    # json_object 模式要求消息里出现 "json" 字样，否则 DashScope 直接报错；
    # 上游判官提示词里含 "JSON object"，满足要求。
    return {"type": "json_object"}


class EvaluationSetupError(RuntimeError):
    """评测栈配置不满足冻结要求。"""


def _assert_not_candidate(model_id: str, role: str) -> None:
    if model_id in CANDIDATE_MODELS:
        raise EvaluationSetupError(
            f"{role} 使用了候选模型 {model_id}：会造成自评偏袒，必须换成候选池外的模型"
        )


def family_collision(model_id: str, role: str) -> str | None:
    """同家族但非同一模型：不阻断，返回一条必须写进报告的警告。

    阻断会把"判官必须存在"这条硬需求也一起否掉（可用的服务商就那么几家）。
    但这条风险不能消失在空气里——它进 `describe()`，进运行 manifest，进报告。
    """
    family = model_family(model_id)
    if model_id not in CANDIDATE_MODELS and family in CANDIDATE_FAMILIES:
        kin = sorted(m for m in CANDIDATE_MODELS if model_family(m) == family)
        return (
            f"{role} {model_id} 与候选模型 {'、'.join(kin)} 同属 {family} 家族："
            f"同源模型可能偏爱自家风格的输出，解读该候选成绩时必须注明此风险"
        )
    return None


def litellm_model(model_id: str) -> str:
    """DashScope 与 DeepSeek 都是 OpenAI 兼容端点，在 litellm 里走 openai/ 前缀。"""
    return f"openai/{model_id}"


@dataclass
class EvalStack:
    """一次评测运行的完整模型配置。写入运行 manifest 后即冻结。"""

    settings: Settings
    agent_model: str
    # None = 用冻结默认，并允许 .env 的 RSA_USER_MODEL 覆盖。
    # 显式传值则**一律优先**：复跑历史结论时要指回旧模拟用户，
    # 若让环境变量压过显式参数，跑出来的就不是你要的那个栈。
    user_model: str | None = None
    judge_model: str | None = None
    agent_extra: dict[str, Any] = field(default_factory=dict)
    # enable_thinking 是 DashScope 的扩展参数，DeepSeek 官方端点不认；
    # 判官在 DashScope 上时才下发，否则会被服务端拒绝。
    judge_thinking_disabled: bool = True
    # 模拟用户的思考开关。**默认 False（保持思考开启）**——模拟用户是冻结评测栈，
    # 改它会让全部历史成绩失效，所以只能由调用方显式打开做对照实验，
    # 绝不能因为"理由充分"就换成默认值。实测它 92% 的输出是推理 token。
    user_thinking_disabled: bool = False
    # 被测 Agent 的思考档位。项目统一 high（见 llm.THINKING_EFFORT）。
    # 注意 DeepSeek 默认就是 thinking+high：显式写出来不改变行为，
    # 但把「我们以为在跑非思考」这个错误认知钉死成可核对的记录。
    agent_thinking_enabled: bool = True
    agent_thinking_effort: str = THINKING_EFFORT
    # 是否把历史 reasoning_content 回传（DeepSeek 文档在带 tools 时要求回传）。
    # 做成开关而不是写死：它每段对话会往请求里多塞约 7600 字符，
    # 对成绩是正是负必须实测，不能靠"文档这么说"就当定论。
    echo_reasoning: bool = True

    def __post_init__(self) -> None:
        # 判官优先用独立端点；没配就退回 DashScope 上的冻结模型。
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
        _assert_not_candidate(self.user_model, "模拟用户")
        _assert_not_candidate(judge, "NL 断言评判模型")
        self._assert_user_endpoint_reachable()
        self._assert_user_thinking_switch_is_supported()
        if "dashscope" not in str(self._judge_routing()["api_base"]):
            self.judge_thinking_disabled = False

    def _assert_user_endpoint_reachable(self) -> None:
        """冻结的模拟用户必须打到它真正所在的平台。

        这条检查存在的理由很具体：`_user_routing()` 在没配 RSA_USER_* 时
        会**静默**退回 DashScope。静默退回对判官是安全的（冻结判官就在 DashScope），
        对模拟用户则不是——冻结模拟用户已经搬到智谱了。
        与其让它去 DashScope 撞一个语焉不详的错误，不如在这里说清楚缺什么。
        """
        if self.user_model != FROZEN_USER_SIMULATOR:
            return  # 调用方显式指定了别的模拟用户（对照实验/复跑历史），自行负责
        api_base = str(self._user_routing()["api_base"])
        if FROZEN_USER_SIMULATOR_HOST not in api_base:
            raise ValueError(
                f"冻结的模拟用户 {FROZEN_USER_SIMULATOR} 只在智谱开放平台上，"
                f"当前会被发往 {api_base}。"
                "请在 .env 里配齐 RSA_USER_BASE_URL / RSA_USER_API_KEY / RSA_USER_MODEL"
                "（三项要么都配、要么都不配）；"
                f"若是要复跑换模拟用户之前的历史结论，请显式传 user_model="
                f"{PREVIOUS_USER_SIMULATOR!r}"
            )

    def _assert_user_thinking_switch_is_supported(self) -> None:
        """`user_thinking_disabled` 在 GLM-5.3 系列上无法实现，必须当场说清楚。

        换成 GLM 后我们**失去了一个对照开关**：GLM-5.3 系列不允许关思考
        （服务端 400 code 1210）。不在这里拦，就会等到 `frozen_args()` 被调用时
        才炸——那已经在跑批中途，钱花了、结果没有。
        """
        if not self.user_thinking_disabled:
            return
        model = str(self.user_model)
        if any(model.startswith(m) for m in ZHIPU_THINKING_ALWAYS_ON):
            raise EvaluationSetupError(
                f"模拟用户 {model} 不支持关闭思考（智谱 GLM-5.3 系列服务端强制思考）。"
                "要做「关思考」的对照实验，请显式指定一个支持关闭的模拟用户，"
                f"例如 user_model={PREVIOUS_USER_SIMULATOR!r}"
            )

    @property
    def judge_is_independent(self) -> bool:
        """判官是否跑在与被测模型不同的服务商上。"""
        return (
            self.settings.has_independent_judge() and self.judge_model == self.settings.judge_model
        )

    def warnings(self) -> list[str]:
        """配置层面的风险提示。空列表代表没有已知风险，不代表没有风险。"""
        judge = str(self.judge_model)
        found = [
            family_collision(str(self.user_model), "模拟用户"),
            family_collision(judge, "NL 断言评判模型"),
        ]
        # 被测与判官同族是更直接的自评偏袒：判官在给自家模型的输出打分。
        # 这条不看候选池，直接比这次运行的两个模型。
        if model_family(self.agent_model) == model_family(judge):
            found.append(
                f"NL 断言评判模型 {judge} 与本次被测 {self.agent_model} "
                f"同属 {model_family(judge)} 家族：判官正在给自家模型的输出打分，"
                f"这是最直接的自评偏袒，该结果不可用于跨家族比较"
            )
        return [w for w in found if w]

    def _routing(self) -> dict[str, Any]:
        return {
            "api_base": self.settings.base_url,
            "api_key": self.settings.dashscope_api_key.get_secret_value(),
        }

    def _user_routing(self) -> dict[str, Any]:
        """模拟用户可以跑在别的平台上；没单独配就退回 DashScope。"""
        if not self.user_is_independent:
            return self._routing()
        assert self.settings.user_api_key is not None  # has_independent_user 已保证
        return {
            "api_base": self.settings.user_base_url,
            "api_key": self.settings.user_api_key.get_secret_value(),
        }

    @property
    def user_is_independent(self) -> bool:
        """模拟用户是否跑在与被测模型不同的平台上。"""
        return self.settings.has_independent_user() and self.user_model == self.settings.user_model

    def _judge_routing(self) -> dict[str, Any]:
        if not self.judge_is_independent:
            return self._routing()
        assert self.settings.judge_api_key is not None  # has_independent_judge 已保证
        return {
            "api_base": self.settings.judge_base_url,
            "api_key": self.settings.judge_api_key.get_secret_value(),
        }

    def _agent_routing(self) -> dict[str, Any]:
        # 与应用侧共用 Settings.agent_endpoint()：两边打到不同服务商的话，
        # 线上表现与评测成绩就没有可比性了。
        base_url, api_key = self.settings.agent_endpoint()
        return {"api_base": base_url, "api_key": api_key}

    @property
    def agent_is_independent(self) -> bool:
        """被测模型是否跑在与模拟用户不同的服务商上。"""
        return self.settings.has_independent_agent()

    def agent_args(self) -> dict[str, Any]:
        """被测 Agent 的调用参数。

        **必须显式带上 temperature**：上游 `TextRunConfig.llm_args_agent` 用
        `default_factory` 提供 `{"temperature": 0.0}`，而 default_factory 只在
        字段缺省时生效——我们每次都传了自己的 dict，等于把那个默认整个替换掉。
        结果是被测 Agent 一直跑在各服务商的默认温度上（通常 0.7~1.0），既不可复现，
        也让跨模型比较落在了不同的采样设置上。这是 2026-09-06 发现并修正的缺陷。
        """
        routing = self._agent_routing()
        is_dashscope = "dashscope" in str(routing["api_base"])
        thinking = ThinkingConfig(
            enabled=self.agent_thinking_enabled,
            effort=self.agent_thinking_effort if self.agent_thinking_enabled else None,
        )
        args: dict[str, Any] = {**routing}
        # 温度对 DeepSeek 思考模式无效，不发；发了只会让 manifest 骗人。
        if not thinking.ignores_temperature(not is_dashscope):
            args["temperature"] = AGENT_TEMPERATURE
        args["extra_body"] = (
            thinking.as_extra_body() if is_dashscope else thinking.as_deepseek_extra_body()
        )
        # agent_extra 放最后：调用方显式给的参数优先级最高
        args.update(self.agent_extra)
        return args

    def frozen_args(self) -> dict[str, Any]:
        """模拟用户的调用参数。"""
        routing = self._user_routing()
        args: dict[str, Any] = {**routing, "temperature": FROZEN_STACK_TEMPERATURE}
        # 思考参数方言按平台选：发错方言不报错，只会被静默忽略。
        thinking = ThinkingConfig(
            enabled=not self.user_thinking_disabled,
            effort=None if self.user_thinking_disabled else THINKING_EFFORT,
        )
        base = str(routing["api_base"])
        if "dashscope" in base:
            args["extra_body"] = thinking.as_extra_body()
        elif "deepseek" in base:
            args["extra_body"] = thinking.as_deepseek_extra_body()
            # DeepSeek 思考模式丢弃 temperature，发了也不生效，不如不发
            if not self.user_thinking_disabled:
                args.pop("temperature", None)
        elif "bigmodel" in base:
            args["extra_body"] = thinking.as_zhipu_extra_body(str(self.user_model))
            # 智谱文档：思考开启时 temperature 固定为 1.0，我们发的 0 不会生效
            if not self.user_thinking_disabled:
                args.pop("temperature", None)
        # 其他平台：不猜方言，只发通用参数。猜错会被静默忽略，比不发更难查。
        return args

    def judge_args(self) -> dict[str, Any]:
        """判官的调用参数：可能指向另一个服务商。

        **必须关掉思考**：上游拿到判官回复后直接 `json.loads(content)`，没有任何
        容错。开着思考模式的模型会把思考内容混进 content，于是同一段输入
        时而返回合法 JSON、时而直接把评测跑崩——`qwen3.7-flash-2026-07-15`
        实测就是这样（输出 6246 tok 里 5570 是 reasoning）。
        判官要的是稳定的结构化输出，不是推理深度。
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
        """给运行 manifest 用的描述。**不含密钥**。"""
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
            "echo_reasoning_content": self.echo_reasoning,
            # 只白名单挑非密字段。用黑名单排除 api_key 是不安全的写法：
            # 上游哪天多一个密钥字段，就会被原样写进 manifest。
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
    """必须在导入 tau2 之前调用。"""
    os.environ["TAU2_DATA_DIR"] = data_dir


def capture_full_usage() -> None:
    """让上游保留缓存与推理 token 字段。

    上游的 `get_response_usage()` 只留了两个字段：

    ```python
    return {"completion_tokens": usage.completion_tokens, "prompt_tokens": usage.prompt_tokens}
    ```

    于是 `prompt_tokens_details.cached_tokens` 与
    `completion_tokens_details.reasoning_tokens` 全被丢掉。这不是小事——
    冒烟实测被测侧 72% 的输入 token 命中了缓存，而缓存命中只按输入价的
    20% 计费；模拟用户 89% 的输出 token 是推理 token，要按输出价付钱。
    两者都不进统计，成本模型就是错的。

    补一句：原始响应其实一直存在 `message.raw_data` 里，所以历史结果**可以回溯**，
    这个补丁只是让新结果的 `usage` 字段自身就够用，不必每次去翻 raw_data。
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
        # DeepSeek 官方端点用的是另一套字段名，两边都收
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
    """让上游把 `reasoning_content` 回传给模型。

    DeepSeek 官方文档（2026-09-06 查阅）：**请求携带 `tools` 参数时**，
    「历史轮次的 `reasoning_content` 均应回传给 API，并会被拼接进上下文」。
    τ²-bench 的 Agent 每一轮都带 tools，而上游的 `to_litellm_messages()`
    只回传 `content` 与 `tool_calls`——推理内容每轮都被丢掉。
    不报错，模型只是每轮都忘记自己刚才想过什么。

    能补上的原因：上游把整个响应存进了 `AssistantMessage.raw_data`，
    `reasoning_content` 一直在那里，只是没被翻译回请求体。

    只在 DeepSeek 端点下有意义（DashScope 不要求回传），但补丁本身是无害的：
    没有 `reasoning_content` 的消息不会被改动。
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
        # 上游按顺序 1:1 转换，两边下标对齐；万一将来不再对齐就整体跳过，
        # 宁可少回传推理，也不能把推理挂到别人的消息上。
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
    """判官一次调用的用量。判官的调用不在 messages 里，不主动记就永远统计不到。"""

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int


class JudgeUsageRecorder:
    """记录 NL 判官的 token 用量。

    存在的理由很直接：判官的调用**不出现在 simulation 的 messages 里**，
    上游也不把它计进 `agent_cost`。此前报告只能写"判官花费未知"——
    未知不等于零，那是必须补上的一笔账。
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
        """包住评判模块里的 `generate`，把每次调用的用量收下来。"""
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
    """把 NL 断言评判模型换成固定的可访问模型，并开始记它的账。

    上游在模块导入时就把默认值绑进了 evaluator 模块的全局名字，
    改 tau2.config 已经来不及，必须直接改那个模块的全局变量。
    """
    from tau2.evaluator import evaluator_nl_assertions as nl

    capture_full_usage()
    nl.DEFAULT_LLM_NL_ASSERTIONS = litellm_model(str(stack.judge_model))
    nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = stack.judge_args()
    recorder = JudgeUsageRecorder()
    recorder.install()
    return recorder


def widen_user_review_severity() -> None:
    """修上游一个会**静默吞掉结论**的自相矛盾。

    `_parse_user_only_review_response()` 明确按
    `{"minor", "critical_helped", "critical_hindered"}` 取 severity，
    提示词也要求模型输出这三个值之一；但数据模型写的是

    ```python
    severity: Optional[Literal["minor", "critical"]]
    ```

    于是只要审查判官真的判出一条严重错误，构造 `UserOnlyReviewError`
    就会抛 ValidationError，被上游兜成一条 `turn_idx=-1、severity=None`、
    reasoning 为「Failed to parse LLM response」的伪错误，
    而 `critical_user_error` 由 severity 反推，于是**恒为 False**。

    结果是这个指标在最关键的地方失灵：`critical_helped`（模拟用户帮了
    不该帮的忙、把考卷改简单）永远报不出来——而那正是我们要用它来防的事。
    2026-09-06 实测复现：24 题里 1 题落进这个陷阱。

    这里把字段放宽到解析器实际会产出的取值，保留 helped/hindered 的区分。
    """
    from tau2.data_model.simulation import UserOnlyReviewError

    field = UserOnlyReviewError.model_fields.get("severity")
    if field is None or getattr(UserOnlyReviewError, "_rsa_severity_widened", False):
        return
    field.annotation = Literal["minor", "critical_helped", "critical_hindered"] | None
    UserOnlyReviewError.model_rebuild(force=True)
    UserOnlyReviewError._rsa_severity_widened = True


def patch_user_reviewer(routing: dict[str, Any]) -> JudgeUsageRecorder:
    """给「模拟用户审查判官」注入路由并记账。

    上游 `UserOnlyReviewer.review()` 只接受 `review_model`，不接受 llm_args——
    没有端点和密钥就直接报「Missing credentials」。和 NL 判官同样的处理：
    包住模块里的 `generate`，补上路由，顺便把用量收下来（这笔调用同样不在
    messages 里，不主动记就统计不到）。
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


# Langfuse 的回调名有两个，**选错的那个不会报错，只会静默不上报**。
#
# 2026-09-07 在 langfuse 4.15.1 + litellm 1.82.6 上实测：
#   "langfuse"      -> 初始化抛 AttributeError（module 'langfuse' has no attribute
#                      'version'，那是 v2 SDK 的属性），litellm 捕获后返回 None
#   "langfuse_otel" -> 正常返回 LangfuseOtelLogger
#
# 上游 tau2 用的正是前者（`llm_utils.py`: `litellm.success_callback = ["langfuse"]`），
# 所以**不能靠把 tau2.config.USE_LANGFUSE 翻成 True 来接**——那样跑完一轮，
# 面板上一条 trace 都没有，而运行本身完全正常，没有任何迹象。
LANGFUSE_CALLBACK = "langfuse_otel"

# 旧回调名。留在这里是为了让它出现在代码搜索里：谁按上游文档去配 "langfuse"，
# 能顺着这个常量找到上面那段说明。
LANGFUSE_BROKEN_CALLBACK = "langfuse"


def enable_langfuse(settings: Settings) -> dict[str, Any]:
    """按需接入 Langfuse，返回状态供 manifest 留痕（**不含密钥**）。

    没配全三项就不启用——半套配置连不上，却要等到跑批中途才暴露。

    **面板上的成本是 0，这是已知的、上游的缺口**（2026-09-07 实测）：

      到达 Langfuse 的有 trace、延迟、模型名、输入/输出 token 数；
      没有到达的是成本——litellm 1.82.6 的 langfuse_otel 集成从不发送
      `langfuse.observation.cost_details.*`（整个模块里没有这个字符串），
      它只把成本写成 `gen_ai.cost.*` 属性，而 Langfuse 的摄取端不认这个键。
      实测 observation 里 `costDetails: {}`、`modelId: null`。

    让 Langfuse 自己按用量算也不行：它收到的 `usageDetails` 只有 input/output，
    **没有缓存明细**，而我们的被测侧 95% 的输入命中缓存（缓存价是全价的 1/30）。
    按全价算会把成本抬高约 5 倍——一个看起来合理的错数字，比 0 更难被发现。

    所以本项目的成本口径是：**Langfuse 只用于 trace / 延迟 / token 观测，
    成本以 scripts/analyze_eval.py 为准**，后者与 litellm 的账交叉核对且严格相等。

    另：我们注册进 litellm 的价格是**人民币**（见 pricing.LITELLM_COST_CURRENCY），
    这一点在 summary.json 里留痕，免得日后有人把它当美元。
    """
    if not settings.has_langfuse():
        return {"langfuse_enabled": False, "langfuse_reason": "未配置 LANGFUSE_* 三项"}

    assert settings.langfuse_public_key and settings.langfuse_secret_key  # has_langfuse 已保证
    host = str(settings.langfuse_host)
    # litellm 的 langfuse_otel 回调从环境变量取配置，没有参数入口。
    # 只在进程内设置，不写盘、不打日志。
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
        # 面板会给这个数字加美元符号，但它是人民币。写进 manifest，
        # 免得日后有人拿面板数字当美元去汇报。
        "langfuse_cost_currency": LITELLM_COST_CURRENCY,
        # 面板成本为 0 是已知缺口，不是本轮免费。成本以 analyze_eval.py 为准。
        "langfuse_cost_reported": False,
        "langfuse_cost_note": (
            "litellm 1.82.6 的 langfuse_otel 不发送 cost_details；"
            "且 Langfuse 收到的用量无缓存明细，自行折算会高估约 5 倍。"
            "成本以 scripts/analyze_eval.py 为准"
        ),
    }


# 面板上的 trace 必须能对回是哪一轮跑的，否则观测和结果各说各话。
# Langfuse 的分组维度是 session：同一个 session_id 的调用在面板上聚成一条会话。
# 我们用**运行目录名**当 session_id——它已经是本项目里一轮运行的唯一标识
# （形如 20260907T141354Z_deepseek-v4-pro_dev），两边不用再维护一张对照表。
LANGFUSE_TAG = "retail-support-agent"

# 会话号放在字典里而不是闭包变量里：补跑会第二次调用 tag_langfuse_session，
# 那时包装函数已经装好了，只需要换号，不该再套一层。
_LANGFUSE_SESSION: dict[str, str] = {}


def tag_langfuse_session(session_id: str) -> str:
    """给本轮所有 LLM 调用打上会话标记，让面板上的 trace 能对回运行目录。

    litellm 的 langfuse_otel 只认 `litellm_params["metadata"]` 里的
    `session_id` / `tags`（见 `LangfuseOtelLogger._set_metadata_attributes`），
    而它们只能**逐次调用**传入——没有全局入口，也没有环境变量
    （唯一的环境变量 `LANGFUSE_TRACING_ENVIRONMENT` 是 dev/prod 那种环境名，
    不是会话号）。上游 tau2 又不给我们传 metadata 的口子，所以在
    `tau2.utils.llm_utils.completion` 这个**三个角色共用的唯一出口**上包一层。

    包在这里而不是包 `litellm.completion`：被测 Agent、模拟用户、两个判官
    最终都从这个名字发起调用，一处覆盖全部；而直接改 litellm 的全局函数
    会波及同进程里任何别的用途。

    `metadata` 是 litellm 自己的参数（在 `get_litellm_params` 的名单里），
    不会被转发给模型服务端，也不受 `litellm.drop_params` 影响。

    没启用 Langfuse 时打这个标记也无害：metadata 只是多一个没人读的字段。
    """
    from tau2.utils import llm_utils

    _LANGFUSE_SESSION["id"] = session_id
    original = llm_utils.completion
    if getattr(original, "_rsa_session_tagger", False):
        return session_id

    def completion_with_session(*args: Any, **kwargs: Any) -> Any:
        metadata = dict(kwargs.get("metadata") or {})
        # setdefault：万一上游哪天自己开始传 metadata，以它的为准，不覆盖。
        metadata.setdefault("session_id", _LANGFUSE_SESSION["id"])
        metadata.setdefault("tags", [LANGFUSE_TAG, _LANGFUSE_SESSION["id"]])
        return original(*args, **{**kwargs, "metadata": metadata})

    completion_with_session._rsa_session_tagger = True  # type: ignore[attr-defined]
    llm_utils.completion = completion_with_session
    return session_id


# 后台记日志的线程要几秒才把 span 建出来，之后批量导出器还压着一批没发。
# 3 秒是实测够用的等待；宁可多等 3 秒，也不要丢掉整轮最后几题的 trace。
LANGFUSE_FLUSH_WAIT_SECONDS = 3.0
LANGFUSE_FLUSH_TIMEOUT_MS = 15_000


def flush_langfuse(wait_seconds: float = LANGFUSE_FLUSH_WAIT_SECONDS) -> bool:
    """跑完后把还压在缓冲区里的 trace 推出去。返回是否真的推成功。

    **不做这一步会静默丢 trace**（2026-09-07 实测）：发一次调用就立刻退出的
    进程，那条 trace 在面板上永远不出现，而进程退出码是 0，日志里什么都没有。
    整轮评测因为跑得久，中途的 span 会被批量导出器按时发走，**丢的是末尾几题**
    ——恰好是"跑完了去面板看看"时最想看的那几条。

    OTEL 的 TracerProvider 本来会在退出时自己 shutdown，但 litellm 的
    langfuse_otel 没有把 provider 设成全局的（`opentelemetry.trace.get_tracer_provider()`
    拿到的仍是 ProxyTracerProvider），所以那条退出路径根本轮不到它。
    唯一的入口是 logger 实例自己持有的 `tracer.span_processor`。

    等待是必要的：litellm 的成功回调跑在另一个线程里，span 还没建出来就
    force_flush 等于什么也没推。这里用固定等待，没有更精确的信号可用。

    `_in_memory_loggers` 是上游私有名字，取不到就返回 False——观测推不出去
    不该让整轮评测失败，所以只报告、不抛错。
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
        except Exception:  # 观测失败不该拖垮评测
            flushed = False
    return flushed


def build_run_config(
    stack: EvalStack,
    task_ids: list[str],
    num_trials: int,
    max_concurrency: int,
    seed: int,
    save_to: str | None,
) -> Any:
    """构造上游的运行配置。任务集与划分一律取自 M0.1 冻结的清单。"""
    # 价格必须在任何调用发生前注册，否则上游 `get_response_cost()` 会一路
    # 捕获异常并 `return 0.0`——那正是 agent_cost 长期恒为 0 的原因。
    # **注册进去的是元，不是美元**，见 pricing.LITELLM_COST_CURRENCY。
    register_with_litellm()
    # 思考模式下 DeepSeek 要求把历史 reasoning_content 回传，上游不会做，我们补上。
    if stack.echo_reasoning:
        echo_reasoning_content()
    from tau2.data_model.simulation import TextRunConfig

    return TextRunConfig(
        domain="retail",
        task_ids=task_ids,
        task_split_name=None,  # 任务由 task_ids 精确指定，不再二次筛选
        agent="llm_agent",
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
