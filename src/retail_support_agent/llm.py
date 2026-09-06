"""模型调用适配：把 OpenAI SDK 包成 Agent 需要的 `chat_fn`。

思考配置在这里集中处理。两家服务商的方言完全不同，**由本模块负责翻译**：

| 服务商   | 开关思考                              | 档位                                   |
|----------|---------------------------------------|----------------------------------------|
| DeepSeek | `thinking.type = enabled/disabled`    | `thinking.reasoning_effort`            |
| DashScope| `enable_thinking = true/false`        | `reasoning_effort` 或 `thinking_budget`|

M0.2 实测发现 DashScope 侧 `reasoning_effort` 与 `thinking_budget`
的互斥只有部分模型强制，且"谁覆盖谁"方向不一致，所以**由本模块保证只传其一**，
不依赖服务端拦截。详见 docs/M0.2_MODEL_CAPABILITY.md。

DeepSeek 官方文档（2026-09-06 查阅）的两条硬约束，直接决定了下面的实现：
- 思考模式**默认开启**，`reasoning_effort` **默认 high**——不传参数不等于不思考；
- 思考模式**不支持** `temperature`/`top_p`/`presence_penalty`/`frequency_penalty`，
  "设置参数不会报错，但也不会生效"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from retail_support_agent.config import SELECTED_MODEL, Settings

# 项目统一的思考档位。`high` 是**两家都合法**的取值，所以可以统一。
THINKING_EFFORT = "high"

# 合法取值两家不同，且**都会在服务端强校验**——取值不对直接 400，整轮跑崩。
# 两份名单都是 2026-09-06 实测得来的，不是从文档抄的：
#   DashScope 的 API 参考页 404，错误信息才吐出完整名单；
#   DeepSeek 文档列了 low/high/max，并说明 medium/xhigh 会被映射成 high。
# 特别注意 `max` 只有 DeepSeek 有：把它发给 DashScope 会
# `'reasoning_effort' must be one of: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh'`。
DEEPSEEK_EFFORTS = frozenset({"low", "high", "max"})
DASHSCOPE_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
# 智谱开放平台（bigmodel.cn，**国内版**，与境外的 z.ai 端点和价格都不同）。
# GLM-5.3 / GLM-5.3-Flash 只有三档；GLM-5.2 支持得更多，但我们不用它当模拟用户。
ZHIPU_EFFORTS = frozenset({"low", "high", "max"})
# GLM-5.3 系列**不允许关思考**：传 disabled 会直接报错。
ZHIPU_THINKING_ALWAYS_ON = ("glm-5.3", "glm-5.3-flash")
# CLI 的候选集取并集；真正的校验发生在下发时，那时才知道打到哪家。
VALID_EFFORTS = DEEPSEEK_EFFORTS | DASHSCOPE_EFFORTS | ZHIPU_EFFORTS


@dataclass(frozen=True)
class ThinkingConfig:
    """思考配置。默认与项目口径一致：开启思考，档位 high。"""

    enabled: bool = True
    # 二选一，不可同时设置——由 __post_init__ 强制
    effort: str | None = THINKING_EFFORT
    budget: int | None = None

    def __post_init__(self) -> None:
        if self.effort is not None and self.budget is not None:
            raise ValueError(
                "reasoning_effort 与 thinking_budget 不能同时设置："
                "部分模型不报错但会静默忽略其中之一，且方向不一致"
            )
        if self.effort is not None and self.effort not in VALID_EFFORTS:
            raise ValueError(
                f"reasoning_effort 只接受 {sorted(VALID_EFFORTS)}，收到 {self.effort!r}"
            )

    def as_extra_body(self) -> dict[str, Any]:
        """DashScope 方言。"""
        body: dict[str, Any] = {"enable_thinking": self.enabled}
        if not self.enabled:
            return body
        if self.effort is not None:
            if self.effort not in DASHSCOPE_EFFORTS:
                raise ValueError(
                    f"DashScope 不接受 reasoning_effort={self.effort!r}，"
                    f"合法取值 {sorted(DASHSCOPE_EFFORTS)}。"
                    "服务端会直接返回 400，整轮评测会中断"
                )
            body["reasoning_effort"] = self.effort
        elif self.budget is not None:
            body["thinking_budget"] = self.budget
        return body

    def as_deepseek_extra_body(self) -> dict[str, Any]:
        """DeepSeek 方言：档位是 `thinking` 对象的子字段，不是顶层参数。"""
        if not self.enabled:
            return {"thinking": {"type": "disabled"}}
        thinking: dict[str, Any] = {"type": "enabled"}
        if self.effort is not None and self.effort not in DEEPSEEK_EFFORTS:
            raise ValueError(
                f"DeepSeek 不接受 reasoning_effort={self.effort!r}，"
                f"合法取值 {sorted(DEEPSEEK_EFFORTS)}"
            )
        if self.budget is not None:
            raise ValueError(
                "DeepSeek 官方端点没有 thinking_budget，只能用 reasoning_effort 控制档位；"
                "静默忽略会让「已限制思考长度」变成错觉"
            )
        if self.effort is not None:
            thinking["reasoning_effort"] = self.effort
        return {"thinking": thinking}

    def as_zhipu_extra_body(self, model_id: str = "") -> dict[str, Any]:
        """智谱方言：`thinking` 与 `reasoning_effort` 都在**顶层**，不嵌套。

        OpenAI SDK 的 `extra_body` 会被合并进请求体顶层，所以放这里即可。
        """
        if not self.enabled:
            if any(model_id.startswith(m) for m in ZHIPU_THINKING_ALWAYS_ON):
                raise ValueError(
                    f"{model_id} 不允许关闭思考：传 thinking.type=disabled 会被服务端拒绝。"
                    "要非思考模式请换 GLM-5.2 或更早的型号"
                )
            return {"thinking": {"type": "disabled"}}
        body: dict[str, Any] = {"thinking": {"type": "enabled"}}
        if self.budget is not None:
            raise ValueError("智谱没有 thinking_budget，只能用 reasoning_effort 控制档位")
        if self.effort is not None:
            if self.effort not in ZHIPU_EFFORTS:
                raise ValueError(
                    f"智谱 GLM-5.3 系列不接受 reasoning_effort={self.effort!r}，"
                    f"合法取值 {sorted(ZHIPU_EFFORTS)}"
                )
            body["reasoning_effort"] = self.effort
        return body

    def ignores_temperature(self, is_deepseek: bool) -> bool:
        """该配置下 temperature 是否会被服务端丢弃。

        DeepSeek 思考模式下温度不生效，且**不报错**——不检查就会一直以为
        自己跑在 temperature=0 的可复现设置上。
        """
        return is_deepseek and self.enabled


@dataclass(frozen=True)
class ModelConfig:
    """一次运行使用的模型配置。冻结后写入运行 manifest。

    默认与 M0.4 的选型结论一致：`deepseek-v4-pro`，思考开启、档位 high。

    此前这里写的是 `ThinkingConfig(enabled=False)`，那是**记错了**：
    我们从未向 DeepSeek 下发任何思考参数，而 DeepSeek 默认就开思考、默认 high，
    所以既往全部运行实际都跑在 thinking+high 上（实测 214 次调用里 190 次有推理）。
    现在把它写成显式配置，让记录与实际一致。

    温度显式写 0.0 而不是"不传"：不传就会落到各服务商的默认温度（通常 0.7~1.0）。
    但要注意它对 DeepSeek 思考模式**无效**——见 `ThinkingConfig.ignores_temperature`。
    """

    model_id: str = SELECTED_MODEL
    thinking: ThinkingConfig = ThinkingConfig(enabled=True, effort=THINKING_EFFORT)
    temperature: float | None = 0.0
    max_completion_tokens: int | None = 2048


def make_chat_fn(settings: Settings, config: ModelConfig) -> Any:
    """构造 Agent 用的 chat_fn。密钥只在这里取一次明文。

    端点由 `Settings.agent_endpoint()` 决定，与评测侧同一处解析——
    应用和评测必须打到同一个服务商，否则线上表现与评测成绩没有可比性。
    """
    base_url, api_key = settings.agent_endpoint()
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=1)
    # 思考参数两家方言不同，各发各的；发错方言会被静默忽略，比报错更难查。
    is_dashscope = settings.agent_is_dashscope()
    extra_body = (
        config.thinking.as_extra_body()
        if is_dashscope
        else config.thinking.as_deepseek_extra_body()
    )
    # 思考模式下 DeepSeek 会丢弃温度，此时干脆不发：发了也不生效，
    # 却会让请求日志看起来像「已固定采样」。
    send_temperature = config.temperature is not None and not config.thinking.ignores_temperature(
        not is_dashscope
    )

    def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        request: dict[str, Any] = {
            "model": config.model_id,
            "messages": messages,
            "tools": tools,
            "extra_body": extra_body,
        }
        if send_temperature:
            request["temperature"] = config.temperature
        if config.max_completion_tokens is not None:
            request["max_completion_tokens"] = config.max_completion_tokens
        return client.chat.completions.create(**request)

    return chat
