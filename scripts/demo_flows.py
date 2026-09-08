"""Run the three demo flows end to end and archive the conversation and database state.

This makes real model calls and costs money.

The M0.3 acceptance criterion is that the three demo flows are reproducible. This
script turns that into one command: each flow records every turn's reply, status and
executed tools, plus the database fingerprint before and after. An unchanged
fingerprint means the write never landed, whatever the model said.

Usage:
    uv run python scripts/demo_flows.py
    uv run python scripts/demo_flows.py --flow cancel
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_support_agent.agent import RetailAgent
from retail_support_agent.api import DEMO_USER_ID
from retail_support_agent.config import Settings
from retail_support_agent.domain import RetailDomain
from retail_support_agent.guardrails import SessionState
from retail_support_agent.llm import ModelConfig, make_chat_fn
from retail_support_agent.tau2_pin import REPO_ROOT

OUTPUT_ROOT = REPO_ROOT / "evaluation" / "demo_runs"

# Orders owned by the demo identity, from the τ²-bench simulated data:
#   #W4135875 pending    -> cancellable
#   #W4817420 delivered  -> returnable
FLOWS: dict[str, list[str]] = {
    "query": [
        "Hi, I would like to see the orders on my account.",
        "What is the status of order #W4135875?",
    ],
    # Writes are confirmed in two stages: the policy requires the agent to explain and
    # obtain agreement, and the system then asks once more with the full arguments. A
    # write flow therefore takes one more turn than a read flow.
    #
    # The return flow gets an extra turn because of a measurement: the selected model
    # deepseek-v4-pro is conservative about writes and, with more arguments, prefers to
    # confirm again rather than act. A return carries 5 item ids, so on the third turn it
    # asked "please reply yes" again, never issued the write, and the database did not
    # move. The same three turns are enough for cancellation, which carries one order id.
    # This is not a guard problem: the guard correctly held the write, and the model
    # simply did not issue the call again.
    "cancel": [
        "I want to cancel order #W4135875, I no longer need it.",
        "Yes, cancel it for that reason.",
        "Confirmed, go ahead.",
    ],
    "return": [
        "Order #W4817420 arrived but does not fit. I would like to return all of it to the "
        "original payment method.",
        "Yes, return everything to the original payment method.",
        "Confirmed, go ahead.",
        "Yes, that is correct, please submit it.",
    ],
    # An attempt on someone else's order, which must be refused.
    "ownership_denied": [
        "Please cancel order #W2378156.",
        "Yes, cancel it.",
    ],
}


@dataclass
class TurnRecord:
    user: str
    reply: str
    status: str
    tool_calls: list[str] = field(default_factory=list)
    pending_confirmation: str | None = None


@dataclass
class FlowRecord:
    flow: str
    db_hash_before: str
    db_hash_after: str
    turns: list[TurnRecord] = field(default_factory=list)

    @property
    def db_changed(self) -> bool:
        return self.db_hash_before != self.db_hash_after


def run_flow(name: str, messages: list[str], settings: Settings, config: ModelConfig) -> FlowRecord:
    """Each flow gets a fresh domain instance so flows cannot affect each other."""
    domain = RetailDomain()
    agent = RetailAgent(make_chat_fn(settings, config), domain, SessionState(user_id=DEMO_USER_ID))
    record = FlowRecord(flow=name, db_hash_before=domain.db_hash(), db_hash_after="")

    print(f"\n=== flow {name} ===", flush=True)
    for message in messages:
        print(f"  user  > {message}")
        result = agent.send(message)
        print(f"  agent > [{result.status}] {result.reply.strip()[:220]}")
        if result.tool_calls:
            print(f"          tools executed: {', '.join(result.tool_calls)}")
        record.turns.append(
            TurnRecord(
                user=message,
                reply=result.reply,
                status=result.status,
                tool_calls=result.tool_calls,
                pending_confirmation=result.pending_confirmation,
            )
        )

    record.db_hash_after = domain.db_hash()
    print(f"  database state: {'changed' if record.db_changed else 'unchanged'}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="M0.3 demo of the three flows (spends real money)")
    parser.add_argument("--flow", choices=sorted(FLOWS), default=None)
    parser.add_argument("--model", default=None, help="override the default model")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(
            f"failed to read configuration (check DASHSCOPE_API_KEY in .env): {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    config = ModelConfig(model_id=args.model) if args.model else ModelConfig()
    selected = {args.flow: FLOWS[args.flow]} if args.flow else FLOWS

    records = [run_flow(name, msgs, settings, config) for name, msgs in selected.items()]

    run_dir = OUTPUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_at": datetime.now(UTC).isoformat(),
        "demo_user_id": DEMO_USER_ID,
        "model": config.model_id,
        "thinking": config.thinking.as_extra_body(),
        "note": (
            "Simulated business data, not a real system. The database fingerprint shows "
            "whether a write actually landed."
        ),
        "flows": [asdict(record) | {"db_changed": record.db_changed} for record in records],
    }
    (run_dir / "flows.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"\narchived: {run_dir / 'flows.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
