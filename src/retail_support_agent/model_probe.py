"""M0.2 模型能力探针。

目的不是"能不能聊天"，而是回答 docs/MODEL_EXPERIMENTS.md 能力检查表里的问题：
工具调用链是否完整、流式是否给 usage、思考开关与预算是否**实际生效**、
互斥规则是否真的被服务端拒绝。

核心纪律：HTTP 200 不等于参数生效。凡是声称"某档位生效"的结论，
都必须有 usage 里的 `reasoning_tokens` 差异或服务端明确报错作为证据。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from openai import APIStatusError, OpenAI
from openai.types.chat import ChatCompletion

from retail_support_agent.config import Settings

ProbeStatus = Literal["ok", "rejected", "error"]

# 探针共用的极小提示词，控制成本；内容与 retail 业务同构但不涉及真实数据。
TINY_PROMPT = "用一句话回答：订单状态查询需要哪一个参数？"

ORDER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_order_details",
        "description": "按订单号查询订单详情",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号，形如 #W0000000"},
            },
            "required": ["order_id"],
        },
    },
}


@dataclass
class ProbeOutcome:
    """单个探针的结果。request 里只有业务参数，绝不含密钥。"""

    name: str
    status: ProbeStatus
    request: dict[str, Any]
    latency_s: float
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    findings: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def build_client(settings: Settings) -> OpenAI:
    """全项目唯一取出密钥明文的地方。"""
    return OpenAI(
        api_key=settings.dashscope_api_key.get_secret_value(),
        base_url=settings.base_url,
        timeout=120.0,
        max_retries=0,  # 重试会掩盖真实的错误类型与限流情况，探针阶段必须关掉
    )


def summarize(completion: ChatCompletion) -> dict[str, Any]:
    """抽取报告需要的字段；完整响应另存原始 JSON。"""
    choice = completion.choices[0] if completion.choices else None
    message = choice.message if choice else None
    usage = completion.usage
    tool_calls = getattr(message, "tool_calls", None) or []

    completion_details = getattr(usage, "completion_tokens_details", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)

    return {
        "response_model_id": completion.model,
        "response_id": completion.id,
        "finish_reason": choice.finish_reason if choice else None,
        "has_content": bool(getattr(message, "content", None)),
        "has_reasoning_content": bool(getattr(message, "reasoning_content", None)),
        "tool_call_names": [tc.function.name for tc in tool_calls],
        "tool_call_arguments": [tc.function.arguments for tc in tool_calls],
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
            "cached_tokens": getattr(prompt_details, "cached_tokens", None),
        },
    }


def describe_error(exc: Exception) -> dict[str, Any]:
    """保存错误类型而不是吞掉；服务端拒绝本身就是有价值的证据。"""
    info: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)[:800]}
    if isinstance(exc, APIStatusError):
        info["http_status"] = exc.status_code
        body = exc.body
        if isinstance(body, dict):
            info["api_code"] = body.get("code")
    return info


def _run(
    client: OpenAI,
    name: str,
    request: dict[str, Any],
) -> tuple[ProbeOutcome, ChatCompletion | None]:
    """发一次非流式请求并封装结果。"""
    started = time.monotonic()
    try:
        completion = client.chat.completions.create(**request)
    except Exception as exc:
        elapsed = time.monotonic() - started
        status: ProbeStatus = "rejected" if isinstance(exc, APIStatusError) else "error"
        return ProbeOutcome(name, status, request, elapsed, error=describe_error(exc)), None
    elapsed = time.monotonic() - started
    return (
        ProbeOutcome(name, "ok", request, elapsed, response=summarize(completion)),
        completion,
    )


def probe_basic(client: OpenAI, model: str) -> ProbeOutcome:
    """账户能否调用该精确模型 ID，服务端返回的模型 ID 是什么。"""
    outcome, _ = _run(
        client,
        "basic",
        {
            "model": model,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_completion_tokens": 64,
        },
    )
    if outcome.response:
        requested, returned = model, outcome.response["response_model_id"]
        outcome.findings = {
            "callable": True,
            "model_id_echoed_exactly": requested == returned,
            "returns_usage": outcome.response["usage"]["total_tokens"] is not None,
        }
    else:
        outcome.findings = {"callable": False}
    return outcome


def probe_tool_call(client: OpenAI, model: str) -> ProbeOutcome:
    """能否发起工具调用，参数是否为合法 JSON 且类型正确。"""
    outcome, _ = _run(
        client,
        "tool_call",
        {
            "model": model,
            "messages": [{"role": "user", "content": "帮我查订单 #W1234567 的状态。"}],
            "tools": [ORDER_TOOL],
            "tool_choice": "auto",
            "max_completion_tokens": 256,
        },
    )
    if outcome.response:
        args_raw = outcome.response["tool_call_arguments"]
        parsed: Any = None
        valid = False
        if args_raw:
            try:
                parsed = json.loads(args_raw[0])
                valid = isinstance(parsed.get("order_id"), str)
            except (json.JSONDecodeError, AttributeError):
                valid = False
        outcome.findings = {
            "emitted_tool_call": bool(args_raw),
            "arguments_parse_ok": parsed is not None,
            "order_id_is_string": valid,
        }
    return outcome


def probe_tool_result_roundtrip(client: OpenAI, model: str) -> ProbeOutcome:
    """把工具结果回传后能否继续对话——这是 Agent 闭环的最低要求。"""
    request = {
        "model": model,
        "messages": [
            {"role": "user", "content": "帮我查订单 #W1234567 的状态。"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_probe_1",
                        "type": "function",
                        "function": {
                            "name": "get_order_details",
                            "arguments": '{"order_id": "#W1234567"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_probe_1",
                "content": '{"order_id": "#W1234567", "status": "pending"}',
            },
        ],
        "tools": [ORDER_TOOL],
        "max_completion_tokens": 128,
    }
    outcome, _ = _run(client, "tool_result_roundtrip", request)
    if outcome.response:
        outcome.findings = {
            "accepts_tool_message": True,
            "answered_after_tool_result": outcome.response["has_content"],
        }
    return outcome


def probe_stream_usage(client: OpenAI, model: str) -> ProbeOutcome:
    """流式是否回传 usage，工具调用能否增量返回。"""
    request: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "帮我查订单 #W1234567 的状态。"}],
        "tools": [ORDER_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 128,
    }
    started = time.monotonic()
    try:
        chunks = list(client.chat.completions.create(**request))
    except Exception as exc:
        elapsed = time.monotonic() - started
        status: ProbeStatus = "rejected" if isinstance(exc, APIStatusError) else "error"
        return ProbeOutcome("stream_usage", status, request, elapsed, error=describe_error(exc))

    elapsed = time.monotonic() - started
    usage_chunks = [c for c in chunks if getattr(c, "usage", None)]
    saw_tool_delta = any(
        getattr(choice.delta, "tool_calls", None)
        for chunk in chunks
        for choice in (chunk.choices or [])
    )
    final_usage = usage_chunks[-1].usage if usage_chunks else None
    return ProbeOutcome(
        "stream_usage",
        "ok",
        request,
        elapsed,
        response={
            "chunk_count": len(chunks),
            "usage_chunk_count": len(usage_chunks),
            "total_tokens": getattr(final_usage, "total_tokens", None),
        },
        findings={
            "streaming_works": bool(chunks),
            "usage_in_stream": bool(usage_chunks),
            "tool_call_streamed_incrementally": saw_tool_delta,
        },
    )


def probe_sampling_params(client: OpenAI, model: str) -> ProbeOutcome:
    """temperature / top_p / seed 是否被接受。被拒绝同样是有效结论。"""
    outcome, _ = _run(
        client,
        "sampling_params",
        {
            "model": model,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "temperature": 0.2,
            "seed": 42,
            "max_completion_tokens": 64,
        },
    )
    outcome.findings = {"accepted": outcome.status == "ok"}
    return outcome


def probe_truncation(client: OpenAI, model: str) -> ProbeOutcome:
    """输出上限是否真的截断，finish_reason 能否区分"说完了"和"被截断"。"""
    outcome, _ = _run(
        client,
        "truncation",
        {
            "model": model,
            "messages": [{"role": "user", "content": "详细介绍退货政策，越长越好。"}],
            "max_completion_tokens": 16,
        },
    )
    if outcome.response:
        outcome.findings = {
            "finish_reason": outcome.response["finish_reason"],
            "signals_length_limit": outcome.response["finish_reason"] == "length",
        }
    return outcome


def probe_thinking_off(client: OpenAI, model: str) -> ProbeOutcome:
    """关闭思考：能否关掉，关掉后 reasoning_tokens 是否归零。"""
    outcome, _ = _run(
        client,
        "thinking_off",
        {
            "model": model,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_completion_tokens": 128,
            "extra_body": {"enable_thinking": False},
        },
    )
    if outcome.response:
        reasoning = outcome.response["usage"]["reasoning_tokens"]
        outcome.findings = {
            "accepted": True,
            "reasoning_tokens": reasoning,
            # 只有真的为 0 或缺省才算关掉；非零说明参数被忽略
            "thinking_actually_off": reasoning in (0, None),
        }
    return outcome


# 服务端在 max_completion_tokens 小于档位映射预算时会报错，并把预算数字写进消息里。
# 这是唯一能从外部读出"某档位到底等于多少 token"的途径。
_BUDGET_IN_ERROR = re.compile(r"thinking_budget \[(\d+)\]")


def probe_effort_budget_mapping(client: OpenAI, model: str, level: str) -> ProbeOutcome:
    """故意把输出上限压到很小，从服务端报错里读出该档位映射的思考预算。

    被拒绝是**预期结果**：报错消息形如
    "max_completion_tokens [512] must be greater than thinking_budget [8192]"，
    其中的 8192 就是该档位的真实预算。不报错则说明该档位预算本身就很小。
    """
    outcome, _ = _run(
        client,
        f"effort_budget_mapping_{level}",
        {
            "model": model,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_completion_tokens": 512,
            "extra_body": {"enable_thinking": True, "reasoning_effort": level},
        },
    )
    mapped: int | None = None
    unsupported = False
    if outcome.error:
        message = str(outcome.error.get("message", ""))
        match = _BUDGET_IN_ERROR.search(message)
        if match:
            mapped = int(match.group(1))
        unsupported = "must be one of" in message
    outcome.findings = {
        "mapped_thinking_budget": mapped,
        "level_unsupported": unsupported,
        # 小上限下就能跑通，说明该档位预算不大于 512
        "fits_in_512": outcome.status == "ok",
    }
    return outcome


def probe_reasoning_effort(client: OpenAI, model: str, level: str) -> ProbeOutcome:
    """逐档试 reasoning_effort，用实际 reasoning_tokens 判断是否真的换了档。

    这里**不设** max_completion_tokens：上限若小于档位映射的预算会直接被拒，
    那测到的是"总输出不够"而不是"该档位不支持"，两者必须区分。
    """
    outcome, _ = _run(
        client,
        f"reasoning_effort_{level}",
        {
            "model": model,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "extra_body": {"enable_thinking": True, "reasoning_effort": level},
        },
    )
    if outcome.response:
        outcome.findings = {
            "accepted": True,
            "reasoning_tokens": outcome.response["usage"]["reasoning_tokens"],
            "completion_tokens": outcome.response["usage"]["completion_tokens"],
        }
    elif outcome.error:
        message = str(outcome.error.get("message", ""))
        outcome.findings = {
            "accepted": False,
            "level_unsupported": "must be one of" in message,
        }
    return outcome


def probe_thinking_budget(client: OpenAI, model: str, budget: int = 64) -> ProbeOutcome:
    """thinking_budget 是否被接受，实际思考 token 是否受该上限约束。"""
    outcome, _ = _run(
        client,
        f"thinking_budget_{budget}",
        {
            "model": model,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_completion_tokens": 512,
            "extra_body": {"enable_thinking": True, "thinking_budget": budget},
        },
    )
    if outcome.response:
        reasoning = outcome.response["usage"]["reasoning_tokens"]
        outcome.findings = {
            "accepted": True,
            "reasoning_tokens": reasoning,
            "within_budget": None if reasoning is None else reasoning <= budget,
        }
    return outcome


def probe_mutual_exclusion(client: OpenAI, model: str) -> ProbeOutcome:
    """文档称 reasoning_effort 与 thinking_budget 互斥，这里验证服务端是否真的拒绝。"""
    outcome, _ = _run(
        client,
        "thinking_mutual_exclusion",
        {
            "model": model,
            "messages": [{"role": "user", "content": TINY_PROMPT}],
            "max_completion_tokens": 256,
            "extra_body": {
                "enable_thinking": True,
                "reasoning_effort": "low",
                "thinking_budget": 64,
            },
        },
    )
    findings: dict[str, Any] = {
        # 被拒绝才说明互斥规则真实存在；接受了则说明其中一个被静默忽略
        "server_rejects_both": outcome.status == "rejected",
        "silently_accepted": outcome.status == "ok",
    }
    if outcome.response:
        reasoning = outcome.response["usage"]["reasoning_tokens"]
        findings["reasoning_tokens"] = reasoning
        # reasoning_effort="low" 映射的预算远大于 64；据此判断哪个参数实际生效
        if reasoning is not None:
            findings["winner"] = "thinking_budget" if reasoning <= 64 else "reasoning_effort"
    outcome.findings = findings
    return outcome
