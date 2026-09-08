"""Re-judge NL assertions on saved trajectories to quantify judge noise.

Worth doing separately because upstream scores assertions all-or-nothing:
`reward = 1.0 if all(met) else 0.0`. One assertion flipping takes a task from
full marks to zero. The noise is amplified, not averaged away.

The agent is not re-run. The judge consumes only the conversation and the
assertions, both already in the result files, so re-judging spends tokens on the
judge alone. It is the cheapest credibility check in the project.

Usage:
    # re-judge with the currently configured judge
    uv run python scripts/recheck_judge.py --run <dir>
    # three passes with the same judge, for self-consistency
    uv run python scripts/recheck_judge.py --run <dir> --repeat 3
    # list which runs contain re-judgeable tasks
    uv run python scripts/recheck_judge.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.config import Settings  # noqa: E402
from retail_support_agent.evaluation import (  # noqa: E402
    EvalStack,
    patch_nl_judge,
    prepare_environment,
)
from retail_support_agent.pricing import deepseek_tier, price_for  # noqa: E402
from retail_support_agent.results_io import load_simulations  # noqa: E402
from retail_support_agent.task_meta import load_task_facts  # noqa: E402

RESULTS_ROOT = REPO / "evaluation" / "results"
DATA_DIR = REPO / "third_party" / "tau2-bench" / "data"


def assertions_of(task_id: str) -> list[str]:
    """Assertion text comes from the upstream task file, not the result file, which
    may hold an empty list."""
    tasks_path = REPO / "third_party" / "tau2-bench" / "data" / "tau2" / "domains" / "retail"
    raw: Any = json.loads((tasks_path / "tasks.json").read_text(encoding="utf-8"))
    tasks = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    for task in tasks:
        if str(task.get("id")) == task_id:
            criteria = task.get("evaluation_criteria") or {}
            return list(criteria.get("nl_assertions") or [])
    return []


def trajectory_of(sim: dict[str, Any]) -> list[Any]:
    """The judge uses only role and content, so a lightweight stand-in is enough and
    the full Message need not be deserialized."""
    return [
        SimpleNamespace(role=str(m.get("role")), content=m.get("content"))
        for m in (sim.get("messages") or [])
    ]


def stored_verdicts(sim: dict[str, Any]) -> dict[str, bool]:
    checks = (sim.get("reward_info") or {}).get("nl_assertions") or []
    return {str(c.get("nl_assertion")): bool(c.get("met")) for c in checks}


def main() -> int:
    # Before parse_args: --help writes to stdout, and a non-UTF-8 console would raise
    # UnicodeEncodeError on non-ASCII characters.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Re-judge NL assertions offline (spends judge-side tokens only)"
    )
    parser.add_argument("--run", default=None, help="run directory under evaluation/results")
    parser.add_argument(
        "--repeat", type=int, default=1, help="passes with the same judge, for self-consistency"
    )
    parser.add_argument(
        "--list", action="store_true", help="list re-judgeable task counts per run and exit"
    )
    args = parser.parse_args()

    facts = load_task_facts()

    if args.list:
        for run_dir in sorted(p for p in RESULTS_ROOT.iterdir() if p.is_dir()):
            sims = load_simulations(run_dir)
            judgeable = sum(
                1
                for s in sims
                if (tid := str(s.get("task_id"))) in facts and facts[tid].invokes_judge
            )
            print(f"{run_dir.name:<50} sims {len(sims):>3}    re-judgeable {judgeable:>3}")
        return 0

    if not args.run:
        raise SystemExit("--run <dir> is required, or use --list to see the runs")

    run_dir = RESULTS_ROOT / args.run
    sims = load_simulations(run_dir)
    targets = [
        s for s in sims if (tid := str(s.get("task_id"))) in facts and facts[tid].invokes_judge
    ]
    if not targets:
        print(
            f"{args.run} contains no task with NL assertions: this run never called the "
            "judge, so there is nothing to re-judge."
        )
        print(
            "Run an evaluation on assertion-bearing tasks first "
            "(scripts/run_eval.py --pilot now selects only those)."
        )
        return 1

    prepare_environment(str(DATA_DIR))
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"failed to read configuration (check .env): {type(exc).__name__}", file=sys.stderr)
        return 2

    # agent_model sends no request during a re-judge; it only makes EvalStack valid.
    stack = EvalStack(settings=settings, agent_model="unused-in-recheck")
    judge_usage = patch_nl_judge(stack)
    for warning in stack.warnings():
        print(f"[warning] {warning}")

    from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator

    print(f"judge: {stack.judge_model}    independent endpoint: {stack.judge_is_independent}")
    print(f"re-judgeable tasks: {len(targets)}    passes per task: {args.repeat}\n")

    records: list[dict[str, Any]] = []
    disagreements = 0
    unstable = 0

    for sim in targets:
        task_id = str(sim.get("task_id"))
        assertions = assertions_of(task_id)
        trajectory = trajectory_of(sim)
        previous = stored_verdicts(sim)

        rounds: list[dict[str, bool]] = []
        broken = 0
        for _ in range(args.repeat):
            # Upstream calls json.loads on the judge's reply with no error handling, so a
            # non-JSON response aborts the run. Here a broken judge is recorded as an
            # observation instead of destroying the re-judge: a judge that cannot produce
            # parseable output is itself a result that must be reported.
            try:
                checks = NLAssertionsEvaluator.evaluate_nl_assertions(trajectory, assertions)
            except json.JSONDecodeError:
                broken += 1
                continue
            rounds.append({c.nl_assertion: bool(c.met) for c in checks})
        if broken:
            print(f"task {task_id:<5} judge returned non-JSON {broken}/{args.repeat} times")
        if not rounds:
            records.append(
                {
                    "task_id": task_id,
                    "assertion_count": len(assertions),
                    "judge_unparseable": broken,
                    "rounds": [],
                    "previous": previous,
                    "disagreements": [],
                    "self_consistent": None,
                }
            )
            continue

        # Self-consistency: whether the same judge reaches the same verdict each pass.
        per_assertion_stable = {
            a: len({r.get(a) for r in rounds}) == 1 for a in {k for r in rounds for k in r}
        }
        task_stable = all(per_assertion_stable.values())
        unstable += 0 if task_stable else 1

        # Disagreements with the original judge.
        first = rounds[0]
        diffs = [a for a, met in first.items() if a in previous and previous[a] != met]
        disagreements += len(diffs)

        old_reward = 1.0 if previous and all(previous.values()) else (1.0 if not previous else 0.0)
        new_reward = 1.0 if all(first.values()) else 0.0

        flag = ""
        if not task_stable:
            flag = "  <- the same judge contradicted itself"
        elif diffs:
            flag = "  <- differs from the original judge"
        print(
            f"task {task_id:<5} assertions {len(assertions)}    "
            f"original {old_reward:.0f} -> re-judged {new_reward:.0f}    "
            f"disagreements {len(diffs)}{flag}"
        )
        for assertion in diffs:
            print(
                f"    original {previous[assertion]} / re-judged {first[assertion]}: "
                f"{assertion[:80]}"
            )

        records.append(
            {
                "task_id": task_id,
                "assertion_count": len(assertions),
                "rounds": rounds,
                "previous": previous,
                "disagreements": diffs,
                "self_consistent": task_stable,
                "judge_unparseable": broken,
            }
        )

    total_assertions = sum(r["assertion_count"] for r in records)
    unparseable = sum(int(r.get("judge_unparseable") or 0) for r in records)
    if unparseable:
        print(
            f"\n[serious] the judge returned non-JSON {unparseable} times. Upstream has no "
            "error handling for this and a real run would abort. This judge cannot be used "
            "for an official run."
        )
    print(
        f"\n{total_assertions} assertions in total, {disagreements} disagreeing with the "
        "original judge"
    )
    if args.repeat > 1:
        print(f"tasks where the same judge contradicted itself: {unstable}/{len(records)}")
    print(
        "More disagreements do not mean the new judge is worse, only that the two are not "
        "interchangeable: historical scores would have to be re-judged as a whole."
    )

    # What this re-judge cost: the judge is the only party sending requests, and the
    # agent side spends nothing.
    usage = judge_usage.describe()
    tier = deepseek_tier()
    price = price_for(str(stack.judge_model))
    cost = (
        price.cost(usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"])
        if price
        else None
    )
    print(
        f"\njudge usage: {usage['judge_calls']} calls, "
        f"input {usage['prompt_tokens']} tok (cached {usage['cached_tokens']}), "
        f"output {usage['completion_tokens']} tok (reasoning {usage['reasoning_tokens']})"
    )
    if cost is None:
        print(f"cost: {stack.judge_model} is not in the price snapshot, cannot be computed")
    else:
        print(f"cost: about {cost:.4f} CNY ({tier} tier; the agent side spends nothing)")

    out = run_dir / f"judge_recheck_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(
        json.dumps(
            {
                "run": args.run,
                "judge": stack.judge_model,
                "judge_independent": stack.judge_is_independent,
                "warnings": stack.warnings(),
                "repeat": args.repeat,
                "total_assertions": total_assertions,
                "disagreements": disagreements,
                "self_inconsistent_tasks": unstable,
                "judge_unparseable_responses": unparseable,
                "judge_usage": usage,
                "judge_cost_cny": cost,
                "deepseek_tier": str(tier),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"details: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
