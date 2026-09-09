"""Paired comparison of two runs on the three axes M5 optimizes against.

Quality, latency and cost, over the tasks both runs actually scored.

Pairing is required because both runs cover the same tasks, so task difficulty
is shared. Comparing averages as two independent groups treats difficulty as
random error and throws away most of the statistical power. McNemar looks only
at tasks one run passed and the other did not, which is the question being asked.

The exact test rather than a chi-squared approximation: the number of
disagreements is usually in the single digits, where chi-squared is unreliable.

Cost appears twice and the two are not interchangeable. *Actual spend* prices every
call at the moment it was made and says what the run cost. *Normalized cost* prices both
runs at one stated rate and is the figure that compares two configurations: DeepSeek's
off-peak rate is half its peak rate, so a run started in the evening is otherwise
"cheaper" than the same configuration started at noon.

Latency is the agent's own model call. Task wall clock is reported next to it and is
never called latency: it contains the simulated user's calls and is inflated by the
number of tasks running at once.

Usage:
    uv run python scripts/compare_runs.py <baseline dir> <candidate dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from math import comb
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.pricing import Tier  # noqa: E402
from retail_support_agent.results_io import load_simulations  # noqa: E402
from retail_support_agent.run_metrics import (  # noqa: E402
    Distribution,
    ModelCall,
    call_latencies,
    model_calls,
    per_successful_task,
    price_varies_with_time,
    recover_clock,
    reference_moment,
    run_cost,
    run_started_at,
)
from retail_support_agent.task_meta import load_task_facts  # noqa: E402

RESULTS_ROOT = REPO / "evaluation" / "results"


@dataclass
class Run:
    """One run, loaded once: everything the comparison needs comes from these files.

    Loading the simulations twice was how the old version worked, and it made the cost
    section and the quality section capable of disagreeing about which tasks were in the
    run.
    """

    summary: dict[str, Any]
    simulations: list[dict[str, Any]]
    calls: list[ModelCall]
    started: datetime | None

    @classmethod
    def load(cls, run_dir: Path) -> Run:
        summary = summary_of(run_dir)
        simulations = load_simulations(run_dir)
        clock = recover_clock(summary, simulations)
        return cls(
            summary=summary,
            simulations=simulations,
            calls=model_calls(simulations, clock, role="assistant"),
            started=run_started_at(summary),
        )

    @property
    def model(self) -> str:
        return str((self.summary.get("stack") or {}).get("agent_model", "?"))

    def rewards(self) -> dict[str, float]:
        """Task id to reward. Unscored simulations are excluded, not counted as 0."""
        out: dict[str, float] = {}
        for sim in self.simulations:
            info = sim.get("reward_info") or {}
            if info.get("reward") is not None:
                out[str(sim.get("task_id"))] = float(info["reward"])
        return out

    def durations(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for sim in self.simulations:
            duration = sim.get("duration")
            if isinstance(duration, int | float) and duration > 0:
                out[str(sim.get("task_id"))] = float(duration)
        return out

    def calls_for(self, tasks: Collection[str] | None) -> list[ModelCall]:
        """The calls belonging to these tasks.

        A comparison charges both runs for the same tasks. Charging each run for whatever
        it happened to complete would report a run that crashed early as the cheap one.
        """
        if tasks is None:
            return self.calls
        return [call for call in self.calls if call.task in tasks]

    def usage(self, tasks: Collection[str] | None = None) -> dict[str, int]:
        totals = {"calls": 0, "prompt": 0, "completion": 0, "cached": 0, "reasoning": 0}
        for call in self.calls_for(tasks):
            totals["calls"] += 1
            totals["prompt"] += call.prompt
            totals["completion"] += call.completion
            totals["cached"] += call.cached
            totals["reasoning"] += call.reasoning
        return totals

    def spend(self, tasks: Collection[str] | None = None) -> float | None:
        """What the agent side actually cost, each call at its own moment."""
        cost = run_cost(self.model, self.calls_for(tasks), fallback=self.started)
        return cost.total if cost.known else None

    def normalized(self, tier: Tier, tasks: Collection[str] | None = None) -> float | None:
        """The same tokens at one fixed rate, so two runs can be compared."""
        cost = run_cost(self.model, self.calls_for(tasks), tier_moment=reference_moment(tier))
        return cost.total if cost.known else None


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


def paired_median_delta(
    base: dict[str, float], cand: dict[str, float], shared: list[str]
) -> float | None:
    """Median of the per-task differences, over tasks both runs measured.

    Pairing matters as much here as it does for quality: task duration varies by a factor
    of five across the task set, and comparing two means would drown a real change in
    that spread.
    """
    deltas = sorted(cand[t] - base[t] for t in shared if t in base and t in cand)
    if not deltas:
        return None
    middle = len(deltas) // 2
    if len(deltas) % 2:
        return deltas[middle]
    return (deltas[middle - 1] + deltas[middle]) / 2


def print_latency(base: Run, cand: Run, shared: Collection[str]) -> None:
    print(f"\n=== latency, over the {len(shared)} shared tasks ===")
    for label, run in (("baseline", base), ("candidate", cand)):
        durations = run.durations()
        per_call = Distribution.of(call_latencies(run.calls_for(shared)))
        per_task = Distribution.of([durations[t] for t in shared if t in durations])
        if per_call is None:
            print(f"  {label:<10} no per-call generation time in these results")
        else:
            print(
                f"  {label:<10} agent call p50 {per_call.p50:6.2f}s  p95 {per_call.p95:6.2f}s"
                f"  max {per_call.maximum:6.2f}s"
                + (
                    f"   task wall p50 {per_task.p50:5.0f}s  p95 {per_task.p95:5.0f}s"
                    if per_task
                    else ""
                )
            )
    delta = paired_median_delta(base.durations(), cand.durations(), list(shared))
    if delta is not None:
        direction = "slower" if delta > 0 else "faster"
        print(
            f"  paired: the candidate is {abs(delta):.1f}s {direction} per task at the "
            f"median of the per-task differences"
        )
    print("  Task wall clock is not latency: it also covers the simulated user and the tools.")


def print_cost(base: Run, cand: Run, shared: Collection[str]) -> None:
    print("\n=== agent-side usage and cost, over the shared tasks ===")
    for label, run in (("baseline", base), ("candidate", cand)):
        u = run.usage(shared)
        spend = run.spend(shared)
        judge = run.summary.get("judge_usage") or {}
        shown = "model not in the price snapshot" if spend is None else f"{spend:.3f} CNY spent"
        print(
            f"  {label:<10} {u['calls']:>4} calls   input {u['prompt']:>8}"
            f" (cached {u['cached']:>7})   output {u['completion']:>6}"
            f" (reasoning {u['reasoning']:>6})   {shown}"
        )
        if judge:
            print(
                f"        judge, whole run: {judge.get('judge_calls', 0)} calls, "
                f"input {judge.get('prompt_tokens', 0)}, "
                f"output {judge.get('completion_tokens', 0)}"
            )

    if not (price_varies_with_time(base.model) or price_varies_with_time(cand.model)):
        return
    tier = Tier.PEAK
    print(f"\n  normalized at the {tier} rate, which is what compares the two configurations:")
    for label, run in (("baseline", base), ("candidate", cand)):
        cost = run.normalized(tier, shared)
        if cost is None:
            print(f"    {label:<10} model not in the price snapshot")
            continue
        rewards = run.rewards()
        passes = sum(1 for task in shared if rewards.get(task, 0.0) >= 1.0)
        per_success = per_successful_task(cost, passes)
        tail = (
            f"   {per_success:.4f} CNY per successful task ({passes} passed)"
            if per_success is not None
            else "   no task passed, so cost per successful task is undefined"
        )
        print(f"    {label:<10} {cost:.3f} CNY{tail}")
    base_cost, cand_cost = base.normalized(tier, shared), cand.normalized(tier, shared)
    if base_cost and cand_cost:
        print(f"    difference: {(cand_cost - base_cost) / base_cost:+.1%} on the same rate")


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

    base_run = Run.load(RESULTS_ROOT / args.baseline)
    cand_run = Run.load(RESULTS_ROOT / args.candidate)
    base, cand = base_run.rewards(), cand_run.rewards()
    shared = sorted(set(base) & set(cand), key=int)
    if not shared:
        raise SystemExit("the two runs share no scored tasks, so they cannot be paired")

    only_base = sorted(set(base) - set(cand), key=int)
    only_cand = sorted(set(cand) - set(base), key=int)
    if only_base or only_cand:
        print(f"[warning] the task sets differ; comparing the {len(shared)} shared tasks")
        print(f"    baseline only: {only_base}    candidate only: {only_cand}")

    facts = load_task_facts()
    base_sum, cand_sum = base_run.summary, cand_run.summary

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

    print_latency(base_run, cand_run, shared)
    print_cost(base_run, cand_run, shared)
    print(f"\n  (cost computed from the pricing.py snapshot; {len(shared)} shared tasks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
