"""Review the behaviour of the simulated user offline. When replacing a simulated
user, this is the only defensible definition of "better".

The agent's score cannot measure the simulated user: a higher score may be caused
by the simulated user getting worse. Measured 2026-09-06: disabling the simulated
user's thinking raised the agent from 18/24 to 23/24, because it split a compound
task into several single tasks and made the test easier. Upstream classifies that
as `critical_helped`.

Uses upstream's `UserOnlyReviewer`, which checks whether every fact the simulated
user stated follows from the task instructions, and grades them:
  critical_helped   - made the task easier (leaked information, solved it for the agent)
  critical_hindered - made the task harder or impossible (wrong or contradictory)
  minor             - wrong but did not affect the outcome

The reviewer model must stay the same across candidates, or the comparison is
between reviewers rather than between simulated users.

Usage:
    uv run python scripts/review_user_sim.py --run <dir>
    uv run python scripts/review_user_sim.py --run <A> --run <B>   # side by side
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.config import Settings  # noqa: E402
from retail_support_agent.evaluation import (  # noqa: E402
    capture_full_usage,
    litellm_model,
    patch_user_reviewer,
    prepare_environment,
)
from retail_support_agent.pricing import price_for  # noqa: E402
from retail_support_agent.results_io import load_simulations  # noqa: E402

RESULTS_ROOT = REPO / "evaluation" / "results"
DATA_DIR = REPO / "third_party" / "tau2-bench" / "data"

# The reviewer shares no origin with the agent under test (deepseek-v4), the judge
# (qwen3.7-flash), or either simulated user being compared (qwen3.6-plus,
# glm-5.3-flash), so no family grades its own. Once chosen it does not change, or
# the comparison stops being about simulated users.
DEFAULT_REVIEWER = "qwen3.8-max"


def load_tasks() -> dict[str, Any]:
    path = DATA_DIR / "tau2" / "domains" / "retail" / "tasks.json"
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    tasks = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    return {str(t["id"]): t for t in tasks}


def review_run(run_dir: Path, reviewer: str, tasks: dict[str, Any]) -> dict[str, Any]:
    from tau2.data_model.simulation import UserInfo
    from tau2.data_model.tasks import Task
    from tau2.evaluator.review_llm_judge_user_only import UserOnlyReviewer
    from tau2.utils.llm_utils import to_tau2_messages

    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    stack = summary.get("stack") or {}
    user_model = str(stack.get("user_simulator", "unknown"))

    from tau2.user.user_simulator import get_global_user_sim_guidelines

    guidelines = get_global_user_sim_guidelines(use_tools=False)

    sims = load_simulations(run_dir)
    records: list[dict[str, Any]] = []
    severities: Counter[str] = Counter()
    tags: Counter[str] = Counter()

    for sim in sims:
        task_id = str(sim.get("task_id"))
        task_raw = tasks.get(task_id)
        if task_raw is None:
            continue
        # Upstream does not persist user_info (`sim["info"]` is empty), so it is
        # reconstructed here. The guidelines come from the same file in the data
        # directory that the run used. The retail simulated user has no tools, hence
        # use_tools=False.
        user_info = UserInfo(
            implementation="user_simulator",
            llm=user_model,
            llm_args=(stack.get("frozen_stack_temperature") and {}) or {},
            global_simulation_guidelines=guidelines,
        )
        messages = to_tau2_messages(sim.get("messages") or [])
        review = UserOnlyReviewer.review(
            user_info=user_info,
            task=Task(**task_raw),
            full_trajectory=messages,
            review_model=litellm_model(reviewer),
        )
        errors = [e.model_dump() for e in review.errors]
        for error in errors:
            severities[str(error.get("severity", "unknown"))] += 1
            for tag in error.get("tags") or []:
                tags[str(tag)] += 1
        records.append(
            {
                "task_id": task_id,
                "user_error": review.user_error,
                "critical_user_error": review.critical_user_error,
                "summary": review.summary,
                "errors": errors,
            }
        )

    scored = [r for r in records if "skipped" not in r]
    return {
        "run": run_dir.name,
        "user_simulator": user_model,
        "reviewer": reviewer,
        "reviewed": len(scored),
        "skipped": len(records) - len(scored),
        "sims_with_any_error": sum(1 for r in scored if r.get("user_error")),
        "sims_with_critical_error": sum(1 for r in scored if r.get("critical_user_error")),
        "errors_by_severity": dict(severities),
        "errors_by_tag": dict(tags),
        "records": records,
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Review the behaviour of the simulated user offline"
    )
    parser.add_argument(
        "--run", action="append", required=True, help="repeatable, for a side-by-side comparison"
    )
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    args = parser.parse_args()

    prepare_environment(str(DATA_DIR))
    capture_full_usage()
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"failed to read configuration (check .env): {type(exc).__name__}", file=sys.stderr)
        return 2

    # The reviewer runs on DashScope: it is neither the agent under test nor a
    # simulated-user candidate, so their quotas are independent.
    usage = patch_user_reviewer(
        {
            "api_base": settings.base_url,
            "api_key": settings.dashscope_api_key.get_secret_value(),
            "temperature": 0.0,
            "extra_body": {"enable_thinking": False},
            "response_format": {"type": "json_object"},
        }
    )

    tasks = load_tasks()
    print(f"reviewer model: {args.reviewer} (must stay the same across candidates)\n")

    results = []
    for name in args.run:
        run_dir = RESULTS_ROOT / name
        result = review_run(run_dir, args.reviewer, tasks)
        results.append(result)
        out = run_dir / f"user_sim_review_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"{result['user_simulator']:<26} details -> {out.name}")

    print(
        f"\n{'simulated user':<26}{'reviewed':>10}{'with errors':>13}{'critical':>10}   by severity"
    )
    for r in results:
        by = r["errors_by_severity"]
        detail = "  ".join(f"{k} {v}" for k, v in sorted(by.items())) or "none"
        print(
            f"{r['user_simulator']:<26}{r['reviewed']:>10}"
            f"{r['sims_with_any_error']:>13}{r['sims_with_critical_error']:>10}   {detail}"
        )
        if r["skipped"]:
            print(f"{'':<26}({r['skipped']} tasks lacked guidelines and were not reviewed)")
        if r["errors_by_tag"]:
            top = sorted(r["errors_by_tag"].items(), key=lambda kv: -kv[1])[:5]
            print(f"{'':<26}most frequent tags: {'  '.join(f'{k} {v}' for k, v in top)}")

    print(
        "\nNote: critical_helped means the test was made easier, the worst class of "
        "error for a simulated user."
    )
    print(
        "      It inflates the agent's score, so a higher agent score is never evidence "
        "that the simulated user is better."
    )
    totals = usage.describe()
    price = price_for(args.reviewer)
    if price is None:
        print(
            f"\n[note] the reviewer {args.reviewer} is not in the price snapshot, so this "
            "review's cost cannot be computed."
        )
    else:
        cost = price.cost(
            totals["prompt_tokens"], totals["completion_tokens"], totals["cached_tokens"]
        )
        print(
            f"\nreview usage: {totals['judge_calls']} calls, "
            f"input {totals['prompt_tokens']} tok (cached {totals['cached_tokens']}), "
            f"output {totals['completion_tokens']} tok, about {cost:.4f} CNY"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
