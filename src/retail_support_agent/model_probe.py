"""M0.2 model capability probes.

The question is not "can it chat" but the checklist in docs/MODEL_EXPERIMENTS.md: is
the tool-call loop complete, does streaming report usage, do the thinking switch and
budget actually take effect, and does the server really enforce the mutual-exclusion
rule.

Core discipline: HTTP 200 does not mean a parameter took effect. Any claim that a
level "works" needs evidence, either a difference in `reasoning_tokens` or an explicit
server error.
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

# A tiny shared prompt to keep probe cost down; shaped like the retail domain but
# touching no real data.
TINY_PROMPT = "In one sentence: which parameter is required to look up an order status?"

ORDER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_order_details",
        "description": "Look up order details by order id",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order id, like #W0000000"},
            },
            "required": ["order_id"],
        },
    },
}


@dataclass
class ProbeOutcome:
    """Result of one probe. `request` holds only business parameters, never a key."""

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
    """The one place in the project that reads the key in plaintext."""
    return OpenAI(
        api_key=settings.dashscope_api_key.get_secret_value(),
        base_url=settings.base_url,
        timeout=120.0,
        # Retries would hide the real error type and rate-limit behaviour, which is
        # exactly what a probe is measuring.
        max_retries=0,
    )


def summarize(completion: ChatCompletion) -> dict[str, Any]:
    """Extract the fields the report needs; the full response is stored as raw JSON."""
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
    """Record the error rather than swallow it: a server refusal is itself evidence."""
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
    """Send one non-streaming request and wrap the result."""
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
    """Whether the account can call this exact model id, and what id the server returns."""
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
    """Whether it emits a tool call with valid, correctly typed JSON arguments."""
    outcome, _ = _run(
        client,
        "tool_call",
        {
            "model": model,
            "messages": [{"role": "user", "content": "Check the status of order #W1234567."}],
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
    """Whether the conversation continues after a tool result is sent back. This is the
    minimum requirement for an agent loop."""
    request = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Check the status of order #W1234567."},
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
    """Whether streaming returns usage, and whether tool calls arrive incrementally."""
    request: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Check the status of order #W1234567."}],
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
    """Whether temperature / top_p / seed are accepted. A refusal is also a result."""
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
    """Whether the output cap truncates, and whether finish_reason distinguishes
    "finished" from "truncated"."""
    outcome, _ = _run(
        client,
        "truncation",
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "Describe the return policy in as much detail as possible.",
                }
            ],
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
    """Whether thinking can be switched off, and whether reasoning_tokens then drops to zero."""
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
            # Only zero or absent counts as off; a non-zero value means the parameter
            # was ignored.
            "thinking_actually_off": reasoning in (0, None),
        }
    return outcome


# When max_completion_tokens is below the budget a level maps to, the server errors and
# includes the budget in the message. That is the only way to read from outside how
# many tokens a level actually means.
_BUDGET_IN_ERROR = re.compile(r"thinking_budget \[(\d+)\]")


def probe_effort_budget_mapping(client: OpenAI, model: str, level: str) -> ProbeOutcome:
    """Deliberately set a tiny output cap and read the level's thinking budget out of
    the server error.

    A refusal is the expected result. The message looks like "max_completion_tokens
    [512] must be greater than thinking_budget [8192]", where 8192 is the level's real
    budget. No error means the level's budget is itself small.
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
        # Succeeding under a small cap means the level's budget is at most 512.
        "fits_in_512": outcome.status == "ok",
    }
    return outcome


def probe_reasoning_effort(client: OpenAI, model: str, level: str) -> ProbeOutcome:
    """Try each reasoning_effort level and use actual reasoning_tokens to tell whether
    the level changed.

    max_completion_tokens is deliberately not set: a cap below the level's mapped
    budget is rejected outright, which would measure "total output too small" rather
    than "level unsupported". The two must not be confused.
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
    """Whether thinking_budget is accepted and whether thinking tokens respect it."""
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
    """The documentation calls reasoning_effort and thinking_budget mutually exclusive.
    This checks whether the server actually refuses."""
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
        # Only a refusal proves the rule is real; acceptance means one of the two was
        # silently ignored.
        "server_rejects_both": outcome.status == "rejected",
        "silently_accepted": outcome.status == "ok",
    }
    if outcome.response:
        reasoning = outcome.response["usage"]["reasoning_tokens"]
        findings["reasoning_tokens"] = reasoning
        # reasoning_effort="low" maps to a budget far above 64, which tells which
        # parameter actually took effect.
        if reasoning is not None:
            findings["winner"] = "thinking_budget" if reasoning <= 64 else "reasoning_effort"
    outcome.findings = findings
    return outcome
