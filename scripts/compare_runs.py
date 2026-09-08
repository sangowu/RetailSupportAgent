"""Paired comparison of two runs: McNemar exact test plus cost and usage.

Pairing is required because both runs cover the same tasks, so task difficulty
is shared. Comparing averages as two independent groups treats difficulty as
random error and throws away most of the statistical power. McNemar looks only
at tasks one run passed and the other did not, which is the question being asked.

The exact test rather than a chi-squared approximation: the number of
disagreements is usually in the single digits, where chi-squared is unreliable.

Usage:
    uv run python scripts/compare_runs.py <baseline dir> <candidate dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from math import comb
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.pricing import deepseek_tier, price_for  # noqa: E402
from retail_support_agent.results_io import load_simulations  # noqa: E402
from retail_support_agent.task_meta import load_task_facts  # noqa: E402

RESULTS_ROOT = REPO / "evaluation" / "results"


def rewards_by_task(run_dir: Path) -> dict[str, float]:
    """Task id to reward. Unscored simulations are excluded, not counted as 0."""
    out: dict[str, float] = {}
    for sim in load_simulations(run_dir):
        info = sim.get("reward_info") or {}
        if info.get("reward") is not None:
            out[str(sim.get("task_id"))] = float(info["reward"])
    return out


def usage_of(run_dir: Path) -> dict[str, int]:
    """Accumulate agent-side usage from messages. Judge usage is recorded
    separately in summary.json."""
    totals = {"calls": 0, "prompt": 0, "completion": 0, "cached": 0, "reasoning": 0}
    for sim in load_simulations(run_dir):
        for message in sim.get("messages") or []:
            if message.get("role") != "assistant":
                continue
            usage = ((message.get("raw_data") or {}).get("usage")) or message.get("usage") or {}
            if not usage:
                continue
            totals["calls"] += 1
            totals["prompt"] += int(usage.get("prompt_tokens") or 0)
            totals["completion"] += int(usage.get("completion_tokens") or 0)
            cached = usage.get("prompt_cache_hit_tokens")
            if cached is None:
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            totals["cached"] += int(cached or 0)
            details = usage.get("completion_tokens_details") or {}
            totals["reasoning"] += int(details.get("reasoning_tokens") or 0)
    return totals


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact test. b and c are the disagreements in each direction;
    tasks both runs agree on carry no information."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail: float = sum(comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def summary_of(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "summary.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def cost_of(model: str, usage: dict[str, int]) -> float | None:
    price = price_for(model)
    if price is None:
        return None
    total: float = price.cost(usage["prompt"], usage["completion"], usage["cached"])
    return total


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Paired comparison of two runs (McNemar exact test)"
    )
    parser.add_argument("baseline", help="baseline run directory name")
    parser.add_argument("candidate", help="candidate run directory name")
    args = parser.parse_args()

    base_dir, cand_dir = RESULTS_ROOT / args.baseline, RESULTS_ROOT / args.candidate
    base, cand = rewards_by_task(base_dir), rewards_by_task(cand_dir)
    shared = sorted(set(base) & set(cand), key=int)
    if not shared:
        raise SystemExit("the two runs share no scored tasks, so they cannot be paired")

    only_base = sorted(set(base) - set(cand), key=int)
    only_cand = sorted(set(cand) - set(base), key=int)
    if only_base or only_cand:
        print(f"[warning] the task sets differ; comparing the {len(shared)} shared tasks")
        print(f"    baseline only: {only_base}    candidate only: {only_cand}")

    facts = load_task_facts()
    base_sum, cand_sum = summary_of(base_dir), summary_of(cand_dir)
    base_model = str((base_sum.get("stack") or {}).get("agent_model", "?"))
    cand_model = str((cand_sum.get("stack") or {}).get("agent_model", "?"))

    def describe(label: str, run: dict[str, Any]) -> None:
        stack = run.get("stack") or {}
        print(
            f"  {label:<10} {stack.get('agent_model', '?'):<20}"
            f" thinking {stack.get('agent_thinking_enabled', '?')}"
            f"/{stack.get('agent_thinking_effort', 'not recorded')}"
            f"  user effort {stack.get('user_simulator_thinking_effort', 'not recorded')}"
            f"  judge format {stack.get('nl_judge_response_format', 'not recorded')}"
        )

    print("=== configuration of the two runs ===")
    describe("baseline", base_sum)
    describe("candidate", cand_sum)

    both = [t for t in shared if base[t] >= 1.0 and cand[t] >= 1.0]
    neither = [t for t in shared if base[t] < 1.0 and cand[t] < 1.0]
    lost = [t for t in shared if base[t] >= 1.0 and cand[t] < 1.0]
    gained = [t for t in shared if base[t] < 1.0 and cand[t] >= 1.0]

    print(f"\n=== paired results ({len(shared)} shared tasks) ===")
    print(
        f"  baseline {len(both) + len(lost)}/{len(shared)}"
        f"    candidate {len(both) + len(gained)}/{len(shared)}"
    )
    print(f"  both passed {len(both)}    neither passed {len(neither)}")
    print(f"  candidate fixed {len(gained)} tasks {gained}")
    print(f"  candidate broke {len(lost)} tasks {lost}")

    p = mcnemar_exact(len(lost), len(gained))
    print(f"\n  McNemar exact test p = {p:.4f}", end="")
    if len(gained) + len(lost) == 0:
        print("    (no task changed; the two are indistinguishable on this set)")
    elif p < 0.05:
        print("    significant")
    else:
        print(
            f"    not significant: {len(gained) + len(lost)} disagreements cannot"
            " support a conclusion"
        )

    judged = [t for t in shared if facts[t].invokes_judge]
    if judged:
        b_j = sum(1 for t in judged if base[t] >= 1.0)
        c_j = sum(1 for t in judged if cand[t] >= 1.0)
        print(
            f"\n  over the {len(judged)} tasks that reach the judge: "
            f"baseline {b_j}    candidate {c_j}"
        )

    print("\n=== agent-side usage and cost ===")
    tier = deepseek_tier()
    for label, run_dir, model, run in (
        ("baseline", base_dir, base_model, base_sum),
        ("candidate", cand_dir, cand_model, cand_sum),
    ):
        u = usage_of(run_dir)
        cost = cost_of(model, u)
        judge = run.get("judge_usage") or {}
        shown = "model not in the price snapshot" if cost is None else f"{cost:.3f} CNY"
        print(
            f"  {label:<10} {u['calls']:>4} calls   input {u['prompt']:>8}"
            f" (cached {u['cached']:>7})   output {u['completion']:>6}"
            f" (reasoning {u['reasoning']:>6})   {shown}"
        )
        if judge:
            print(
                f"        judge {judge.get('judge_calls', 0)} calls, "
                f"input {judge.get('prompt_tokens', 0)}, "
                f"output {judge.get('completion_tokens', 0)}"
            )
    print(f"\n  (DeepSeek tier: {tier}; cost computed from the pricing.py snapshot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
