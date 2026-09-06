"""模型调用适配：把 OpenAI SDK 包成 Agent 需要的 `chat_fn`。

思考配置在这里集中处理。M0.2 实测发现 `reasoning_effort` 与 `thinking_budget`
的互斥只有部分模型强制，且"谁覆盖谁"方向不一致，所以**由本模块保证只传其一**，
不依赖服务端拦截。详见 docs/M0.2_MODEL_CAPABILITY.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from retail_support_agent.config import REFERENCE_MODEL, Settings


@dataclass(frozen=True)
class ThinkingConfig:
    """思考配置。M0.3 只用参照配置跑通流程，最终配置由 M0.4 实验决定。"""

    enabled: bool = True
    # 二选一，不可同时设置——由 as_extra_body 强制
    effort: str | None = None
    budget: int | None = None

    def __post_init__(self) -> None:
        if self.effort is not None and self.budget is not None:
            raise ValueError(
                "reasoning_effort 与 thinking_budget 不能同时设置："
                "部分模型不报错但会静默忽略其中之一，且方向不一致"
            )

    def as_extra_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"enable_thinking": self.enabled}
        if not self.enabled:
            return body
        if self.effort is not None:
            body["reasoning_effort"] = self.effort
        elif self.budget is not None:
            body["thinking_budget"] = self.budget
        return body


@dataclass(frozen=True)
class ModelConfig:
    """一次运行使用的模型配置。冻结后写入运行 manifest。"""

    model_id: str = REFERENCE_MODEL
    thinking: ThinkingConfig = ThinkingConfig(enabled=False)
    temperature: float | None = None
    max_completion_tokens: int | None = 2048


def make_chat_fn(settings: Settings, config: ModelConfig) -> Any:
    """构造 Agent 用的 chat_fn。密钥只在这里取一次明文。"""
    client = OpenAI(
        api_key=settings.dashscope_api_key.get_secret_value(),
        base_url=settings.base_url,
        timeout=120.0,
        max_retries=1,
    )

    def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        request: dict[str, Any] = {
            "model": config.model_id,
            "messages": messages,
            "tools": tools,
            "extra_body": config.thinking.as_extra_body(),
        }
        if config.temperature is not None:
            request["temperature"] = config.temperature
        if config.max_completion_tokens is not None:
            request["max_completion_tokens"] = config.max_completion_tokens
        return client.chat.completions.create(**request)

    return chat
