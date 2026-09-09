"""M5.3 gate zero: may one tools-carrying conversation mix thinking on and off?

DeepSeek requires that every historical `reasoning_content` be sent back when a request
carries tools, and answers 400 when it is not. The documentation does not say what that
means for a conversation whose earlier turns ran with thinking disabled and so produced
no `reasoning_content` at all. Per-turn routing is only possible if that is allowed, so
it is measured before anything is designed around it.

Three turns against the real endpoint, disabled then enabled then disabled, each request
replaying the whole history under the documented rule. The prompts are tiny and the
output caps low; the whole probe costs about a cent.

Results go to evaluation/capability_checks/<UTC timestamp>/thinking_mix.json. No key
appears in the output.

Usage:
    uv run python scripts/probe_thinking_mix.py
    uv run python scripts/probe_thinking_mix.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from openai import OpenAI  # noqa: E402

from retail_support_agent.config import SELECTED_MODEL, Settings  # noqa: E402
from retail_support_agent.model_probe import describe_error  # noqa: E402

OUTPUT_ROOT = REPO / "evaluation" / "capability_checks"

ORDER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_order_details",
        "description": "Look up one order by its id.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
}

# Turn by turn: what the user says, and whether thinking is on for that request. The
# order matters. A disabled turn first is what creates the history with no
# reasoning_content in it, which is the case the documentation leaves open.
TURNS: tuple[tuple[str, bool], ...] = (
    ("Look up order #W1234567 for me.", False),
    ("Thanks. Is that order still cancellable under the policy?", True),
    ("Fine. Just tell me the order id again in one line.", False),
)


def thinking_body(enabled: bool) -> dict[str, Any]:
    """The provider's own switch. Thinking mode rejects temperature and the other
    sampling parameters, so a disabled turn is the only one that may carry them; sending
    none at all keeps the two turns comparable."""
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


def assistant_entry(message: Any) -> dict[str, Any]:
    """Rebuild the assistant turn for the history, carrying reasoning_content back.

    The documentation is explicit that it must be returned in full whenever the request
    carries tools, even for a turn that called no tool. A turn that ran with thinking
    disabled has none, and whether its absence is accepted is the question here.
    """
    entry: dict[str, Any] = {"role": "assistant", "content": message.content}
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        entry["reasoning_content"] = reasoning
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ]
    return entry


def run_probe(client: OpenAI, model: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []

    for index, (text, thinking) in enumerate(TURNS, start=1):
        messages.append({"role": "user", "content": text})
        request: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "tools": [ORDER_TOOL],
            "tool_choice": "auto",
            "max_completion_tokens": 512,
            "extra_body": thinking_body(thinking),
        }
        step: dict[str, Any] = {
            "turn": index,
            "thinking": thinking,
            "history_has_reasoning": any("reasoning_content" in m for m in messages),
            "history_assistant_turns": sum(1 for m in messages if m["role"] == "assistant"),
        }
        try:
            completion = client.chat.completions.create(**request)
        except Exception as exc:
            step["status"] = "error"
            step["error"] = describe_error(exc)
            steps.append(step)
            return {"model": model, "steps": steps, "mixed_modes_accepted": False}

        message = completion.choices[0].message
        reasoning = getattr(message, "reasoning_content", None)
        step["status"] = "ok"
        step["returned_reasoning_content"] = bool(reasoning)
        step["reasoning_chars"] = len(reasoning) if reasoning else 0
        step["finish_reason"] = completion.choices[0].finish_reason
        step["emitted_tool_call"] = bool(message.tool_calls)
        usage = completion.usage
        if usage is not None:
            step["usage"] = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "reasoning_tokens": getattr(
                    getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
                ),
            }
        steps.append(step)

        messages.append(assistant_entry(message))
        if message.tool_calls:
            for call in message.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            {"order_id": "#W1234567", "status": "pending"}, ensure_ascii=False
                        ),
                    }
                )

    mixed = any(s["thinking"] for s in steps) and any(not s["thinking"] for s in steps)
    return {
        "model": model,
        "steps": steps,
        "mixed_modes_accepted": mixed and all(s["status"] == "ok" for s in steps),
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=SELECTED_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        for index, (text, thinking) in enumerate(TURNS, start=1):
            mode = "enabled" if thinking else "disabled"
            print(f"turn {index}: thinking {mode}, user says {text!r}")
        return 0

    settings = Settings()  # type: ignore[call-arg]
    base_url, api_key = settings.agent_endpoint()
    client = OpenAI(base_url=base_url, api_key=api_key)

    started = datetime.now(UTC)
    result = run_probe(client, args.model)
    result["probed_at"] = started.isoformat()
    result["base_url"] = base_url
    result["question"] = (
        "may a tools-carrying conversation mix thinking-enabled and thinking-disabled "
        "turns, given that historical reasoning_content must be returned in full"
    )

    out_dir = OUTPUT_ROOT / started.strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "thinking_mix.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    for step in result["steps"]:
        mode = "enabled " if step["thinking"] else "disabled"
        if step["status"] != "ok":
            print(f"turn {step['turn']} thinking {mode}: FAILED {step['error']}")
            continue
        print(
            f"turn {step['turn']} thinking {mode}: ok, "
            f"reasoning_content returned {step['returned_reasoning_content']}, "
            f"history already carried reasoning {step['history_has_reasoning']}"
        )
    verdict = "accepted" if result["mixed_modes_accepted"] else "REFUSED"
    print(f"\nmixed thinking modes in one tools conversation: {verdict}")
    print(f"raw record: {path}")
    return 0 if result["mixed_modes_accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
