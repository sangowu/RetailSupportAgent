"""Run an evaluation on τ²-bench Retail. This spends real money.

Start with a tiny sample, estimate the cost, and only then scale up.

Usage:
    # 2-task smoke run, both carrying NL assertions
    uv run python scripts/run_eval.py --pilot
    # 6 tasks, stratified by judge coverage
    uv run python scripts/run_eval.py --split dev --sample 6
    # independent verification, never used for tuning
    uv run python scripts/run_eval.py --split holdout
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.config import SELECTED_MODEL, Settings  # noqa: E402
from retail_support_agent.evaluation import (  # noqa: E402
    EvalStack,
    EvaluationSetupError,
    build_run_config,
    enable_langfuse,
    flush_langfuse,
    patch_nl_judge,
    prepare_environment,
    tag_langfuse_session,
)
from retail_support_agent.llm import THINKING_EFFORT, VALID_EFFORTS  # noqa: E402
from retail_support_agent.task_meta import load_task_facts, split_composition  # noqa: E402
from retail_support_agent.tau2_pin import load_task_set  # noqa: E402

OUTPUT_ROOT = REPO / "evaluation" / "results"
DATA_DIR = REPO / "third_party" / "tau2-bench" / "data"


def local_utc_offset_seconds() -> float:
    """How far ahead of UTC this machine's naive timestamps are."""
    offset = datetime.now().astimezone().utcoffset()
    return round(offset.total_seconds()) if offset is not None else 0.0


def select_tasks(split: str, limit: int | None, sample: int | None, pilot: bool) -> list[str]:
    """Tasks always come from the M0.1 frozen lists, sliced in a fixed order for
    reproducibility.

    Naive `--limit` truncation has a hazard that was measured, not assumed: the first
    two dev tasks have no NL assertions, and upstream returns 1.0 for an empty assertion
    list without calling the judge. The first smoke run hit exactly this, and its 2/2
    included an NL_ASSERTION component that was never evaluated. Smoke runs therefore
    pick only tasks that really reach the judge, and sampling is stratified by judge
    coverage.
    """
    facts = load_task_facts()
    ids: list[str] = load_task_set(split)["task_ids"]

    if pilot:
        # dev is the only set that may be run repeatedly; take tasks that reach the judge.
        judge_ids = [t for t in load_task_set("dev")["task_ids"] if facts[t].invokes_judge]
        if len(judge_ids) < 2:
            raise SystemExit("fewer than 2 dev tasks carry NL assertions; cannot run a smoke test")
        return judge_ids[:2]

    if sample:
        # Preserve the split's ratio of assertion-bearing to other tasks, in a fixed,
        # reproducible order.
        judge_ids = [t for t in ids if facts[t].invokes_judge]
        other_ids = [t for t in ids if not facts[t].invokes_judge]
        n_judge = max(1, round(sample * len(judge_ids) / len(ids))) if judge_ids else 0
        n_judge = min(n_judge, len(judge_ids), sample)
        picked = set(judge_ids[:n_judge]) | set(other_ids[: sample - n_judge])
        return [t for t in ids if t in picked]  # back to the frozen list's order

    return ids[:limit] if limit else ids


def plan_phases(task_ids: list[str], early_stop: int | None) -> list[list[str]]:
    """Split the tasks into a probe phase and the remainder.

    Choosing a model in one round means there is no fallback tier, but there is also no
    reason to run every task for a configuration that is clearly failing. Run the first
    N and stop if they all score zero. An invalid or unreachable N falls back to a
    single phase.
    """
    if not early_stop or early_stop <= 0 or early_stop >= len(task_ids):
        return [task_ids]
    return [task_ids[:early_stop], task_ids[early_stop:]]


def average_reward(simulations: list[Any]) -> float | None:
    """Average reward over scored simulations, or None when nothing is scored."""
    rewards = [
        float(info.reward)
        for sim in simulations
        if (info := getattr(sim, "reward_info", None)) is not None
        and getattr(info, "reward", None) is not None
    ]
    return sum(rewards) / len(rewards) if rewards else None


def summarize(simulations: list[Any], expected: int) -> dict[str, Any]:
    """Extract the numbers the report needs from upstream results. Rewards are not
    recomputed here.

    `expected` is how many sessions should have run (tasks x trials). It is passed in to
    reconcile: upstream drops infrastructure failures from the denominator, so losing 5
    of 40 tasks yields 0.886 instead of 0.875. The loss raises no error and improves the
    score, so the gap is recorded explicitly rather than left for someone to find in a
    log.
    """
    rewards: list[float] = []
    terminations: dict[str, int] = {}
    unscored: list[str] = []
    agent_cost = 0.0

    for sim in simulations:
        info = getattr(sim, "reward_info", None)
        if info is not None and getattr(info, "reward", None) is not None:
            rewards.append(float(info.reward))
        else:
            unscored.append(str(getattr(sim, "task_id", "?")))
        reason = str(getattr(sim, "termination_reason", "unknown"))
        terminations[reason] = terminations.get(reason, 0) + 1
        cost = getattr(sim, "agent_cost", None)
        if isinstance(cost, int | float):
            agent_cost += float(cost)

    incomplete = len(rewards) < expected
    return {
        "agent_cost_reported_by_upstream": agent_cost,
        "simulation_count": len(simulations),
        "expected_count": expected,
        "scored_count": len(rewards),
        # With a non-zero gap, avg_reward is the average over what ran, not the run's
        # score. analyze_eval sees incomplete=True and refuses to report it directly.
        "incomplete": incomplete,
        "unscored_task_ids": sorted(set(unscored), key=lambda t: (len(t), t)),
        "avg_reward": (sum(rewards) / len(rewards)) if rewards else None,
        "pass_count": sum(1 for r in rewards if r >= 1.0),
        "termination_reasons": terminations,
    }


def main() -> int:
    # Before parse_args: argparse writes --help straight to stdout, and a non-UTF-8
    # console would raise UnicodeEncodeError on characters such as τ².
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="τ²-bench Retail evaluation (spends real money)")
    parser.add_argument("--model", default=SELECTED_MODEL, help="model under test")
    parser.add_argument(
        "--split",
        default="dev",
        choices=["dev", "holdout", "regression", "full"],
        help="full = all 114 tasks (the standard τ²-bench Retail figure); dev/holdout are subsets",
    )
    parser.add_argument("--limit", type=int, default=None, help="truncate to the first N tasks")
    parser.add_argument(
        "--sample", type=int, default=None, help="sample N tasks stratified by judge coverage"
    )
    parser.add_argument(
        "--pilot", action="store_true", help="2-task smoke run (both carry NL assertions)"
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--seed", type=int, default=300)
    parser.add_argument(
        "--thinking",
        default=THINKING_EFFORT,
        choices=sorted(VALID_EFFORTS),
        help=f"reasoning_effort for the agent under test, default {THINKING_EFFORT}",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="disable thinking for the agent under test (DeepSeek has it on by default)",
    )
    parser.add_argument(
        "--early-stop",
        type=int,
        default=None,
        metavar="N",
        help="run the first N tasks and stop if the average reward is at or below the threshold",
    )
    parser.add_argument(
        "--early-stop-below",
        type=float,
        default=0.001,
        help="early-stop threshold, default 0.001, i.e. stop only if the first N all score zero",
    )
    parser.add_argument(
        "--no-reasoning-echo",
        action="store_true",
        help="do not send historical reasoning_content back (ablation; DeepSeek documents "
        "that it should be sent when the request carries tools)",
    )
    parser.add_argument(
        "--user-no-thinking",
        action="store_true",
        help=(
            "disable thinking for the simulated user (comparison only; makes scores "
            "incomparable with existing runs). Not available for the frozen "
            "glm-5.3-flash: the Zhipu GLM-5.3 family forces thinking, and this errors at "
            "startup with a pointer to the previous simulated user"
        ),
    )
    parser.add_argument(
        "--guarded",
        action="store_true",
        help=(
            "put this project's guarded agent under test instead of upstream's agent. "
            "They are different subjects with separate baselines and their scores are "
            "not directly comparable"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    task_ids = select_tasks(args.split, args.limit, args.sample, args.pilot)
    if args.dry_run:
        print(
            f"would run {len(task_ids)} tasks x {args.trials} trials = "
            f"{len(task_ids) * args.trials} sessions"
        )
        shown = ", ".join(task_ids[:10])
        more = " ..." if len(task_ids) > 10 else ""
        print(f"model under test: {args.model}; tasks: {shown}{more}")
        facts = load_task_facts()
        judged = [t for t in task_ids if facts[t].invokes_judge]
        print(f"tasks that actually reach the NL judge: {len(judged)}/{len(task_ids)} -> {judged}")
        print(f"composition of this split: {split_composition(args.split)}")
        return 0

    prepare_environment(str(DATA_DIR))
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"failed to read configuration (check .env): {type(exc).__name__}", file=sys.stderr)
        return 2
    # Only the exception type is printed, never its message: it can contain the value of
    # an environment variable.

    # EvalStack translates the thinking configuration into the endpoint's dialect:
    # DeepSeek uses thinking.{type,reasoning_effort}, DashScope uses enable_thinking plus
    # reasoning_effort. The wrong dialect is silently ignored, so extra_body is never
    # assembled by hand here.
    try:
        stack = EvalStack(
            settings=settings,
            agent_model=args.model,
            agent_thinking_enabled=not args.no_thinking,
            agent_thinking_effort=args.thinking,
            echo_reasoning=not args.no_reasoning_echo,
            user_thinking_disabled=args.user_no_thinking,
        )
    except (ValueError, EvaluationSetupError) as exc:
        # These are configuration errors, not crashes: a traceback would bury the one
        # useful sentence. The message contains endpoints and model ids, never keys.
        print(f"evaluation stack misconfigured: {exc}", file=sys.stderr)
        return 2
    if args.user_no_thinking:
        print(
            "[note] the simulated user has thinking disabled: this changes the frozen "
            "stack, and these scores are not comparable with existing runs.",
            flush=True,
        )
    # Langfuse is enabled only when fully configured; the reason is printed otherwise.
    # Note the cost convention: dashboard numbers are CNY despite the dollar sign.
    langfuse_status = enable_langfuse(settings)
    if langfuse_status["langfuse_enabled"]:
        print(
            f"Langfuse enabled ({langfuse_status['langfuse_host']}, "
            f"callback {langfuse_status['langfuse_callback']}). "
            "The dashboard has traces, latency and tokens only; cost is always 0 there "
            "(an upstream gap, not a free run). Cost comes from analyze_eval.py.",
            flush=True,
        )
    else:
        print(f"Langfuse not enabled: {langfuse_status['langfuse_reason']}", flush=True)

    # The returned recorder accounts for the judge: its calls are not in messages and are
    # otherwise never counted.
    judge_usage = patch_nl_judge(stack)
    for warning in stack.warnings():
        print(f"[warning] {warning}", flush=True)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    subject = "guarded" if args.guarded else "upstream"
    # The subject goes in the directory name: scores of the two subjects are not
    # comparable, and mixed directory names get confused eventually.
    run_dir = OUTPUT_ROOT / f"{run_id}_{args.model.replace('/', '_')}_{args.split}_{subject}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Dashboard traces map back to the run directory: the session id is the directory
    # name, so no lookup table is needed.
    session_id = tag_langfuse_session(run_dir.name)
    if langfuse_status["langfuse_enabled"]:
        langfuse_status["langfuse_session_id"] = session_id
        print(
            f"Langfuse session id = {session_id} (dashboard: Tracing -> Sessions)",
            flush=True,
        )

    # Early stop: run the first N tasks and stop spending if the result is clearly
    # failing. Phases are written separately and merged back into one run by results_io.
    phases = plan_phases(task_ids, args.early_stop)

    thinking_note = "off" if args.no_thinking else f"on, effort={args.thinking}"
    print(
        f"running {len(task_ids)} tasks x {args.trials} trials, model {args.model} "
        f"(thinking {thinking_note})",
        flush=True,
    )
    if len(phases) > 1:
        print(
            f"early stop enabled: run {len(phases[0])} tasks first and stop if the average "
            f"reward is <= {args.early_stop_below}",
            flush=True,
        )

    # Note: tau2.run.run_tasks is a legacy compatibility shim with the same name as
    # runner.batch.run_tasks but a different signature. run_domain is the documented
    # high-level entry point; it filters by the config's task_ids and computes metrics.
    from tau2.run import run_domain

    started = time.monotonic()
    all_simulations: list[Any] = []
    stopped_early = False
    completed_ids: list[str] = []

    for index, phase_ids in enumerate(phases, start=1):
        save_name = "simulations.json" if len(phases) == 1 else f"phase{index}.json"
        config = build_run_config(
            stack,
            task_ids=phase_ids,
            num_trials=args.trials,
            max_concurrency=args.concurrency,
            seed=args.seed,
            save_to=str(run_dir / save_name),
            guarded=args.guarded,
        )
        if len(phases) > 1:
            print(f"\nphase {index}/{len(phases)}: {len(phase_ids)} tasks", flush=True)
        results = run_domain(config)
        all_simulations.extend(getattr(results, "simulations", []) or [])
        completed_ids.extend(phase_ids)

        if index < len(phases):
            avg = average_reward(all_simulations)
            if avg is not None and avg <= args.early_stop_below:
                stopped_early = True
                print(
                    f"\nstopping early: the first {len(completed_ids)} tasks average "
                    f"{avg:.4f} <= {args.early_stop_below}; the remaining "
                    f"{len(task_ids) - len(completed_ids)} tasks will not run.",
                    flush=True,
                )
                print(
                    "This is a 'clearly failing' judgement, not a ranking conclusion.",
                    flush=True,
                )
                break
            shown = "no scored records" if avg is None else f"{avg:.4f}"
            print(f"first {len(completed_ids)} tasks average {shown}; continuing.", flush=True)

    elapsed = time.monotonic() - started

    # The explicit flush is required: without it the last tasks' traces sit in the
    # buffer and are lost, while the exit code is 0 and the log says nothing
    # (measured 2026-09-07).
    if langfuse_status["langfuse_enabled"]:
        langfuse_status["langfuse_flushed"] = flush_langfuse()
        if not langfuse_status["langfuse_flushed"]:
            print(
                "[warning] not all Langfuse traces were flushed; the dashboard may be "
                "missing the last few tasks.",
                file=sys.stderr,
            )

    summary = {
        "run_id": run_id,
        "run_at": datetime.now(UTC).isoformat(),
        "wall_seconds": round(elapsed, 1),
        # Message timestamps in the raw results are naive local time from this machine.
        # Recording the offset is what lets a later analysis place them on a timeline
        # without guessing, which matters because DeepSeek's rate depends on the hour.
        "local_utc_offset_seconds": local_utc_offset_seconds(),
        "task_ids": task_ids,
        "completed_task_ids": completed_ids,
        "stopped_early": stopped_early,
        "num_trials": args.trials,
        # The subject under test must be recorded: upstream's agent and the guarded agent
        # are different subjects whose scores are not comparable. Without this, resuming
        # would fill a run with the other subject and mix both into one directory, and
        # the merged average would look entirely normal.
        "agent_under_test": subject,
        "stack": stack.describe(),
        "observability": langfuse_status,
        "judge_usage": judge_usage.describe(),
        "results": summarize(all_simulations, expected=len(completed_ids) * args.trials),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(summary["results"], ensure_ascii=False, indent=2))
    results = summary["results"]
    if results["incomplete"]:
        missing = results["expected_count"] - results["scored_count"]
        print(
            f"\n[serious] {missing} sessions in this run are unscored. The avg_reward="
            f"{results['avg_reward']} above is the average over the rest, not this run's score.",
            file=sys.stderr,
        )
        print(f"       unscored tasks: {results['unscored_task_ids']}", file=sys.stderr)
        print(f"       termination reasons: {results['termination_reasons']}", file=sys.stderr)
        print(
            "       resume: uv run python scripts/resume_run.py "
            f"{run_dir.name} (the evaluation stack is verified field by field and a "
            "mismatch is refused)",
            file=sys.stderr,
        )
    print(f"judge usage: {judge_usage.describe()}")
    print(f"\nraw results: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
