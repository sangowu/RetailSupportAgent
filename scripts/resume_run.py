"""Re-run the unscored tasks of a run and merge the results into its directory.

Why this exists: provider failures (quota, rate limits, timeouts) leave individual
tasks unrun, and upstream drops them from the denominator, so losing 5 of 40 tasks
yields 0.886 instead of 0.875. The loss raises no error and improves the score.
`run_eval.py` records the gap in summary.json, `analyze_eval.py` refuses to report a
score while a gap exists, and this script closes it. Re-running the whole set also
works, but costs another full run.

The evaluation stack is verified field by field: a difference in model, thinking level,
judge, or judge JSON format aborts. Resumed tasks must run on the same stack as the
original, or merging them back adulterates the result.

Usage:
    uv run python scripts/resume_run.py <run directory>
    uv run python scripts/resume_run.py <run directory> --dry-run
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

from retail_support_agent.config import Settings  # noqa: E402
from retail_support_agent.evaluation import (  # noqa: E402
    EvalStack,
    build_run_config,
    enable_langfuse,
    flush_langfuse,
    patch_nl_judge,
    prepare_environment,
    tag_langfuse_session,
)
from retail_support_agent.results_io import is_scored, load_simulations  # noqa: E402

RESULTS_ROOT = REPO / "evaluation" / "results"
DATA_DIR = REPO / "third_party" / "tau2-bench" / "data"

LINE_BREAK = chr(10)

# Run parameters of the original: the caller chose these, and a resume copies them.
# There is nothing to verify about a value we supply ourselves.
REPLAYED_FIELDS = (
    "agent_model",
    "agent_thinking_enabled",
    "agent_thinking_effort",
    "echo_reasoning_content",
)

# Values the original run derived from the environment rather than from arguments:
# frozen constants, .env, and the judge's capabilities. These are what drift silently
# between runs, through a changed frozen simulated user, an edited RSA_JUDGE_MODEL, or a
# judge whose model change alters the JSON format. A resume therefore does not supply
# them; they are resolved fresh and compared against the original.
DERIVED_FIELDS = (
    "user_simulator",
    "user_simulator_thinking_effort",
    "nl_judge",
    "nl_judge_response_format",
    "agent_base_url",
    "user_simulator_base_url",
    "nl_judge_base_url",
)


def unscored_task_ids(run_dir: Path) -> list[str]:
    """Tasks in this run with no reward. Already-recovered ones are excluded, because
    load_simulations deduplicates."""
    return [str(sim.get("task_id")) for sim in load_simulations(run_dir) if not is_scored(sim)]


def resolve_subject(run_dir: Path, summary: dict[str, Any]) -> bool:
    """Whether this run's subject is the guarded agent. Aborts when it cannot be told.

    Upstream's agent and the guarded agent are different subjects whose scores are not
    comparable. Resuming with the other subject mixes both into one directory, and the
    merged average looks entirely normal, which is exactly the failure this script
    exists to prevent (see the module docstring).

    Runs before M0.6 have no such field, and at that time there was only one subject, so
    they are treated as upstream. But if the directory name says guarded while the field
    is missing, the two signals disagree and the resume is refused.
    """
    recorded = summary.get("agent_under_test")
    looks_guarded = run_dir.name.endswith("_guarded")
    if recorded is None:
        if looks_guarded:
            raise SystemExit(
                f"the directory name {run_dir.name} says this run used the guarded agent, "
                "but summary.json has no agent_under_test field, so the subject cannot be "
                "confirmed. Refusing to resume: the wrong subject would mix two "
                "incomparable scores into one run."
            )
        return False
    if recorded not in {"upstream", "guarded"}:
        raise SystemExit(f"unrecognized subject under test: {recorded!r}")
    if looks_guarded != (recorded == "guarded"):
        raise SystemExit(
            "the directory name and the recorded subject disagree: "
            f"name={run_dir.name}, recorded={recorded}. Refusing to resume."
        )
    is_guarded: bool = recorded == "guarded"
    return is_guarded


def rebuild_stack(settings: Settings, described: dict[str, Any]) -> EvalStack:
    """Rebuild the original stack and verify the parts we do not supply.

    Run parameters (REPLAYED_FIELDS) are copied from the manifest: they are inputs, so
    copying them guarantees equality and "verifying" them would prove nothing. What
    matters is DERIVED_FIELDS: the simulated user, the judge, the judge's JSON format
    and the three endpoints, all derived from frozen constants and .env, all able to
    change quietly between runs. Those are not passed to the constructor; they resolve
    against the current environment and are then compared.

    Keys take no part in the comparison and are never written to disk.
    """
    stack = EvalStack(
        settings=settings,
        agent_model=str(described["agent_model"]),
        agent_thinking_enabled=bool(described.get("agent_thinking_enabled", True)),
        agent_thinking_effort=str(described.get("agent_thinking_effort", "high")),
        echo_reasoning=bool(described.get("echo_reasoning_content", True)),
    )
    now = stack.describe()
    drift = [
        f"{key}: original={described.get(key)!r} now={now.get(key)!r}"
        for key in DERIVED_FIELDS
        if key in described and now.get(key) != described.get(key)
    ]
    if drift:
        raise SystemExit(
            "the evaluation stack differs from the original run; refusing to resume "
            "(merging would make this run's score a mixture of two stacks):"
            + LINE_BREAK
            + "  "
            + (LINE_BREAK + "  ").join(drift)
            + LINE_BREAK
            + "To change the stack deliberately, re-run the whole set into a new directory."
        )
    return stack


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Re-run the unscored tasks of a run (spends real money)"
    )
    parser.add_argument("run", help="directory name under evaluation/results")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_dir = RESULTS_ROOT / args.run
    if not run_dir.is_dir():
        raise SystemExit(f"run directory not found: {run_dir}")
    summary: dict[str, Any] = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    missing = unscored_task_ids(run_dir)
    if not missing:
        print("no unscored tasks in this run; nothing to resume.")
        return 0
    print(f"{len(missing)} tasks to resume: {missing}")

    if args.dry_run:
        return 0

    prepare_environment(str(DATA_DIR))
    settings = Settings()  # type: ignore[call-arg]
    stack = rebuild_stack(settings, summary.get("stack", {}))
    guarded = resolve_subject(run_dir, summary)
    subject = "the guarded agent" if guarded else "the upstream agent"
    print(f"evaluation stack matches the original field by field; subject: {subject}. Resuming.")

    judge_usage = patch_nl_judge(stack)
    # Resumed traces must land in the original session, or the dashboard gains an orphan
    # session containing a handful of tasks.
    langfuse_status = enable_langfuse(settings)
    session_id = tag_langfuse_session(run_dir.name)
    if langfuse_status["langfuse_enabled"]:
        print(f"Langfuse session id = {session_id} (the original run's session)")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    config = build_run_config(
        stack,
        task_ids=missing,
        num_trials=int(summary.get("num_trials", 1)),
        max_concurrency=args.concurrency,
        seed=int(summary.get("seed", 300) or 300),
        save_to=str(run_dir / f"resume_{stamp}.json"),
        guarded=guarded,
    )
    from tau2.run import run_domain

    run_domain(config)

    if langfuse_status["langfuse_enabled"]:
        langfuse_status["langfuse_flushed"] = flush_langfuse()

    still = unscored_task_ids(run_dir)
    recovered = [t for t in missing if t not in still]
    # The resume itself is recorded: who resumed which tasks and when, or the run's
    # results cannot be explained afterwards.
    (run_dir / f"resume_{stamp}.meta.json").write_text(
        json.dumps(
            {
                "resumed_at": datetime.now(UTC).isoformat(),
                "requested_task_ids": missing,
                "recovered_task_ids": recovered,
                "still_unscored_task_ids": still,
                "judge_usage": judge_usage.describe(),
                "observability": langfuse_status,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\nrecovered {len(recovered)}/{len(missing)} tasks.")
    if still:
        print(
            f"still unscored: {still}. The same failure persists; diagnose before retrying.",
            file=sys.stderr,
        )
        return 1
    print("this run is now complete. Re-analyze with:")
    print(f"  uv run python scripts/analyze_eval.py --run {run_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
