"""Model adapter: wraps the OpenAI SDK into the `chat_fn` the agent needs.

Thinking configuration is handled here. The providers use different dialects and this
module translates between them:

| Provider  | Enable thinking                     | Level                                  |
|-----------|-------------------------------------|----------------------------------------|
| DeepSeek  | `thinking.type = enabled/disabled`  | `thinking.reasoning_effort`            |
| DashScope | `enable_thinking = true/false`      | `reasoning_effort` or `thinking_budget`|

M0.2 measurements showed that on DashScope the exclusivity of `reasoning_effort` and
`thinking_budget` is enforced only by some models, and which one wins is
inconsistent. This module therefore guarantees only one is sent rather than relying
on server-side rejection. See docs/M0.2_MODEL_CAPABILITY.md.

Two constraints from DeepSeek's documentation (read 2026-09-06) shape the code below:
- thinking is on by default and `reasoning_effort` defaults to high, so sending no
  parameters does not mean no thinking;
- thinking mode does not support `temperature`/`top_p`/`presence_penalty`/
  `frequency_penalty`: "setting them does not error, and does not take effect".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from retail_support_agent.config import SELECTED_MODEL, Settings

# The project-wide thinking level. `high` is valid on both providers.
THINKING_EFFORT = "high"

# Valid values differ per provider and are validated server-side: a wrong value
# returns 400 and aborts the run. Both lists were measured on 2026-09-06 rather than
# copied from documentation: DashScope's API reference page 404s and only the error
# message lists the full set; DeepSeek documents low/high/max and states that
# medium/xhigh are mapped to high.
# Note `max` exists only on DeepSeek. Sending it to DashScope yields
# `'reasoning_effort' must be one of: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh'`.
DEEPSEEK_EFFORTS = frozenset({"low", "high", "max"})
DASHSCOPE_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
# Zhipu open platform (bigmodel.cn, the mainland China service; its endpoints and
# prices differ from z.ai). GLM-5.3 / GLM-5.3-Flash accept three levels; GLM-5.2
# accepts more, but it is not used as the simulated user.
ZHIPU_EFFORTS = frozenset({"low", "high", "max"})
# The GLM-5.3 family cannot disable thinking: sending disabled errors out.
ZHIPU_THINKING_ALWAYS_ON = ("glm-5.3", "glm-5.3-flash")
# The CLI accepts the union; the real check happens at request time, when the target
# provider is known.
VALID_EFFORTS = DEEPSEEK_EFFORTS | DASHSCOPE_EFFORTS | ZHIPU_EFFORTS


@dataclass(frozen=True)
class ThinkingConfig:
    """Thinking configuration. Defaults match the project setting: enabled, level high."""

    enabled: bool = True
    # Exactly one of the two; enforced in __post_init__.
    effort: str | None = THINKING_EFFORT
    budget: int | None = None

    def __post_init__(self) -> None:
        if self.effort is not None and self.budget is not None:
            raise ValueError(
                "reasoning_effort and thinking_budget cannot both be set: some models "
                "silently ignore one of them, and which one is inconsistent"
            )
        if self.effort is not None and self.effort not in VALID_EFFORTS:
            raise ValueError(
                f"reasoning_effort must be one of {sorted(VALID_EFFORTS)}, got {self.effort!r}"
            )

    def as_extra_body(self) -> dict[str, Any]:
        """DashScope dialect."""
        body: dict[str, Any] = {"enable_thinking": self.enabled}
        if not self.enabled:
            return body
        if self.effort is not None:
            if self.effort not in DASHSCOPE_EFFORTS:
                raise ValueError(
                    f"DashScope rejects reasoning_effort={self.effort!r}; "
                    f"valid values are {sorted(DASHSCOPE_EFFORTS)}. "
                    "The server returns 400 and the whole run aborts"
                )
            body["reasoning_effort"] = self.effort
        elif self.budget is not None:
            body["thinking_budget"] = self.budget
        return body

    def as_deepseek_extra_body(self) -> dict[str, Any]:
        """DeepSeek dialect: the level is a field of the `thinking` object, not top level."""
        if not self.enabled:
            return {"thinking": {"type": "disabled"}}
        thinking: dict[str, Any] = {"type": "enabled"}
        if self.effort is not None and self.effort not in DEEPSEEK_EFFORTS:
            raise ValueError(
                f"DeepSeek rejects reasoning_effort={self.effort!r}; "
                f"valid values are {sorted(DEEPSEEK_EFFORTS)}"
            )
        if self.budget is not None:
            raise ValueError(
                "The DeepSeek endpoint has no thinking_budget; only reasoning_effort "
                "controls the level. Ignoring it silently would turn "
                "'thinking length is capped' into a false belief"
            )
        if self.effort is not None:
            thinking["reasoning_effort"] = self.effort
        return {"thinking": thinking}

    def as_zhipu_extra_body(self, model_id: str = "") -> dict[str, Any]:
        """Zhipu dialect: `thinking` and `reasoning_effort` are both top level, not nested.

        The OpenAI SDK merges `extra_body` into the top level of the request body.
        """
        if not self.enabled:
            if any(model_id.startswith(m) for m in ZHIPU_THINKING_ALWAYS_ON):
                raise ValueError(
                    f"{model_id} cannot disable thinking: thinking.type=disabled is "
                    "rejected server-side. Use GLM-5.2 or earlier for non-thinking mode"
                )
            return {"thinking": {"type": "disabled"}}
        body: dict[str, Any] = {"thinking": {"type": "enabled"}}
        if self.budget is not None:
            raise ValueError(
                "Zhipu has no thinking_budget; only reasoning_effort controls the level"
            )
        if self.effort is not None:
            if self.effort not in ZHIPU_EFFORTS:
                raise ValueError(
                    f"Zhipu GLM-5.3 rejects reasoning_effort={self.effort!r}; "
                    f"valid values are {sorted(ZHIPU_EFFORTS)}"
                )
            body["reasoning_effort"] = self.effort
        return body

    def ignores_temperature(self, is_deepseek: bool) -> bool:
        """Whether the server will discard temperature under this configuration.

        DeepSeek ignores temperature in thinking mode without erroring, so without
        this check a run would appear to be reproducible at temperature=0 when it is not.
        """
        return is_deepseek and self.enabled


@dataclass(frozen=True)
class ModelConfig:
    """Model configuration for one run. Written into the run manifest once frozen.

    Defaults match the M0.4 selection: `deepseek-v4-pro`, thinking enabled at high.

    This previously read `ThinkingConfig(enabled=False)`, which was wrong: no thinking
    parameter was ever sent to DeepSeek, and DeepSeek defaults to thinking at high, so
    every earlier run actually ran with thinking+high (190 of 214 measured calls
    carried reasoning). The configuration is now explicit so the record matches reality.

    Temperature is written as 0.0 rather than omitted: omitting it falls back to each
    provider's default, usually 0.7-1.0. It has no effect in DeepSeek thinking mode;
    see `ThinkingConfig.ignores_temperature`.
    """

    model_id: str = SELECTED_MODEL
    thinking: ThinkingConfig = ThinkingConfig(enabled=True, effort=THINKING_EFFORT)
    temperature: float | None = 0.0
    max_completion_tokens: int | None = 2048


def make_chat_fn(settings: Settings, config: ModelConfig) -> Any:
    """Build the agent's chat_fn. The key is read in plaintext only here.

    The endpoint comes from `Settings.agent_endpoint()`, the same resolution the
    evaluation uses: application and evaluation must reach the same provider, or
    production behaviour and benchmark scores are not comparable.
    """
    base_url, api_key = settings.agent_endpoint()
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=1)
    # Each provider gets its own dialect. The wrong dialect is silently ignored, which
    # is harder to find than an error.
    is_dashscope = settings.agent_is_dashscope()
    extra_body = (
        config.thinking.as_extra_body()
        if is_dashscope
        else config.thinking.as_deepseek_extra_body()
    )
    # DeepSeek discards temperature in thinking mode, so it is not sent at all: it
    # would have no effect while making the request log look like fixed sampling.
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
