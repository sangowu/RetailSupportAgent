"""Compute usage and cost from raw evaluation results, and project a larger run.

Upstream accounts through litellm. Since M0.5 the price snapshot is registered with
litellm under the model names actually sent (see pricing.register_with_litellm), so
`agent_cost` is no longer always 0; cost is still computed here independently from
token usage times the price snapshot, and the two paths cross-check each other. Runs
from before registration still report 0 on the upstream side.

Three items that were missing and measurably change the conclusion:

1. Cache hits: 72% of agent-side input tokens hit the implicit cache, billed at 20% of
   the input rate. Full-price arithmetic overstates cost.
2. Reasoning tokens: 89% of the simulated user's output tokens are reasoning, billed at
   the output rate, and were previously not counted.
3. The judge: its calls are not in `messages` and must be recorded separately by
   JudgeUsageRecorder.

Usage is read from `message.raw_data.usage`. Upstream's `usage` field keeps only two
numbers, but the full response has always been in raw_data, so historical results can
be recovered.

Usage:
    uv run python scripts/analyze_eval.py                    # analyze the latest run
    uv run python scripts/analyze_eval.py --run <dir name>
    uv run python scripts/analyze_eval.py --project 74       # project the cost of 74 tasks
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.evaluation import FROZEN_USER_SIMULATOR  # noqa: E402
from retail_support_agent.pricing import (  # noqa: E402
    DASHSCOPE_PRICES,
    PRICE_SNAPSHOT_DATE,
    cache_ratio_is_known,
    price_for,
)
from retail_support_agent.results_io import load_simulations  # noqa: E402

RESULTS_ROOT = REPO / "evaluation" / "results"


@dataclass
class RoleUsage:
    """Accumulated usage for one role (agent under test, simulated user)."""

    calls: int = 0
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    reasoning: int = 0
    missing_usage: int = 0
    # Calls where the server did not report a cache field at all. Distinct from
    # "reported, and it was 0": the first is unknown, charged at full price, which is
    # conservative; the second is a confirmed miss. Measured: qwen3.6-plus always
    # returns null for prompt_tokens_details.cached_tokens, and conflating the two
    # would make a reported "0% hit rate" read as "this model has no cache benefit".
    cache_unreported: int = 0

    @property
    def cache_hit_rate(self) -> float:
        return self.cached / self.prompt if self.prompt else 0.0

    @property
    def cache_is_unknown(self) -> bool:
        """No call reported a cache field, so the hit rate is unknown, not zero."""
        return self.calls > 0 and self.cache_unreported == self.calls


def latest_run() -> Path:
    runs = sorted(p for p in RESULTS_ROOT.iterdir() if p.is_dir()) if RESULTS_ROOT.is_dir() else []
    if not runs:
        raise SystemExit("no evaluation results found; run scripts/run_eval.py first")
    return runs[-1]


def _usage_of(message: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer the full response in raw_data: it carries cache and reasoning detail that
    upstream's usage field does not."""
    raw = message.get("raw_data")
    if isinstance(raw, dict):
        raw_usage = raw.get("usage")
        if isinstance(raw_usage, dict):
            return dict(raw_usage)
    usage = message.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


def _detail(usage: dict[str, Any], group: str, key: str) -> int:
    details = usage.get(group) or {}
    if not isinstance(details, dict):
        return 0
    return int(details.get(key) or 0)


def _cached_tokens(usage: dict[str, Any]) -> int | None:
    """Cache hits, or None when the server did not report the field at all.

    Three field names can appear: the flat field added by our patch, OpenAI's nested
    detail, and DeepSeek's prompt_cache_hit_tokens. Only if all are absent is it
    counted as unreported.
    """
    for key in ("cached_tokens", "prompt_cache_hit_tokens"):
        value = usage.get(key)
        if value is not None:
            return int(value)
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details["cached_tokens"])
    return None


def token_breakdown(simulations: list[dict[str, Any]]) -> dict[str, RoleUsage]:
    """Split usage by message role: assistant is the agent under test, user is the
    simulated user.

    NL-assertion judge calls are not in `messages` and are not counted here; they are
    recorded at run time by JudgeUsageRecorder and written to summary.json.
    """
    roles: dict[str, RoleUsage] = collections.defaultdict(RoleUsage)
    for sim in simulations:
        for message in sim.get("messages") or []:
            role = str(message.get("role", "unknown"))
            usage = _usage_of(message)
            if usage is None:
                if role in ("assistant", "user"):
                    roles[role].missing_usage += 1
                continue
            entry = roles[role]
            entry.calls += 1
            entry.prompt += int(usage.get("prompt_tokens") or 0)
            entry.completion += int(usage.get("completion_tokens") or 0)
            cached = _cached_tokens(usage)
            if cached is None:
                entry.cache_unreported += 1
            else:
                entry.cached += cached
            entry.reasoning += int(
                usage.get("reasoning_tokens")
                or _detail(usage, "completion_tokens_details", "reasoning_tokens")
                or 0
            )
    return dict(roles)


def format_cost(model: str, usage: RoleUsage) -> str:
    """A model with an unknown cache rate gets a range, not a falsely precise number."""
    price = price_for(model)
    if price is None:
        return f"{model} is not in the price snapshot; cost cannot be computed"
    high = price.cost(usage.prompt, usage.completion, usage.cached)
    if cache_ratio_is_known(model) or not usage.cached:
        return f"{high:.4f} CNY"
    low = price.cost_floor(usage.prompt, usage.completion, usage.cached)
    return f"{low:.4f} - {high:.4f} CNY (cache-hit rate unpublished, so only a range)"


def print_usage_table(roles: dict[str, RoleUsage]) -> None:
    print(
        f"\n{'role':<12}{'calls':>7}{'input':>10}{'cached':>10}"
        f"{'hit':>7}{'output':>9}{'reasoning':>11}"
    )
    for role, usage in sorted(roles.items()):
        print(
            f"{role:<12}{usage.calls:>7}{usage.prompt:>10}{usage.cached:>10}"
            f"{usage.cache_hit_rate:>6.0%}{usage.completion:>9}{usage.reasoning:>11}"
        )
        if usage.missing_usage:
            print(
                f"{'':<12}({usage.missing_usage} messages had no usage record and are "
                "excluded; usually locally generated messages such as the opening line, "
                "which called no model)"
            )
        if usage.cache_unreported:
            scope = "every" if usage.cache_is_unknown else f"{usage.cache_unreported}"
            print(
                f"{'':<12}({scope} call returned no cache field: the hit rate is unknown, "
                "not zero. Charged at full price, which is conservative)"
            )


def role_cost(model: str, usage: RoleUsage) -> float | None:
    """Cost for this role, or None when the model is not in the price snapshot. None
    must not be treated as zero."""
    price = price_for(model)
    if price is None:
        return None
    return price.cost(usage.prompt, usage.completion, usage.cached)


def print_judge_cost(summary: dict[str, Any], judge_model: str) -> float | None:
    judge_usage = summary.get("judge_usage")
    if not judge_usage:
        print(
            f"NL judge ({judge_model}): no usage recorded for this run, which predates "
            "judge accounting."
        )
        print("    Not being counted is not the same as being free; this was a blank line.")
        return None
    prompt = int(judge_usage.get("prompt_tokens", 0))
    completion = int(judge_usage.get("completion_tokens", 0))
    cached = int(judge_usage.get("cached_tokens", 0))
    price = price_for(judge_model)
    cost = price.cost(prompt, completion, cached) if price else None
    tail = f"about {cost:.4f} CNY" if cost is not None else "price unavailable"
    print(
        f"NL judge ({judge_model}): {judge_usage.get('judge_calls', 0)} calls, "
        f"input {prompt} tok (cached {cached}), output {completion} tok, {tail}"
    )
    return cost


# The three flows this project declares (lookup / cancel / return) cover 42 of the 114
# tasks. These two labels together are those 42: read_only (lookups) and in_scope_write
# (writes limited to cancellation and returns). The other 72 involve modifying orders,
# exchanges, address and payment changes, which this project does not claim to support.
IN_SCOPE_LABELS = ("read_only", "in_scope_write")


def print_scope_breakdown(simulations: list[dict[str, Any]]) -> None:
    """Report the score layered by business scope.

    Both figures are real and neither stands in for the other: the full-set score is the
    standard τ²-bench Retail score, and the 42-task subset is performance on the flows
    this project declares. Reporting only the first hides the second; reporting only the
    second means the run can no longer be called standard τ²-bench. M0.1 requires both.

    This section only has enough sample when the full set was run: only 9 of the 40
    holdout tasks fall in the subset, and 9 tasks support no conclusion, so a small
    sample is stated rather than scored.
    """
    labels_path = REPO / "evaluation" / "task_sets" / "scope_labels.json"
    if not labels_path.is_file():
        return
    labels: dict[str, str] = json.loads(labels_path.read_text(encoding="utf-8"))["labels"]

    scored = [
        (str(sim.get("task_id")), float(reward))
        for sim in simulations
        if (reward := (sim.get("reward_info") or {}).get("reward")) is not None
    ]
    if not scored:
        return

    buckets: dict[str, list[float]] = collections.defaultdict(list)
    for task_id, reward in scored:
        buckets[labels.get(task_id, "unlabeled")].append(reward)

    print("\nBy business scope (labels from evaluation/task_sets/scope_labels.json)")
    order = ["read_only", "in_scope_write", "out_of_scope_write", "no_action", "unlabeled"]
    names = {
        "read_only": "read-only (lookups)",
        "in_scope_write": "in-scope writes (cancel/return)",
        "out_of_scope_write": "out-of-scope writes (modify/exchange/address/payment)",
        "no_action": "no scored action upstream",
        "unlabeled": "unlabeled",
    }
    for key in order:
        got = buckets.get(key)
        if not got:
            continue
        passed = sum(1 for r in got if r >= 1.0)
        print(f"  {names[key]:<52}{passed:3d}/{len(got):<4d} = {passed / len(got):.4f}")

    in_scope = [r for key in IN_SCOPE_LABELS for r in buckets.get(key, [])]
    if not in_scope:
        return
    passed = sum(1 for r in in_scope if r >= 1.0)
    if len(in_scope) < 20:
        print(
            f"\n  Only {len(in_scope)} tasks of the declared-flows subset are in this run. "
            "Sample too small to score separately; the full 42 need a full-set run."
        )
        return
    print(
        f"\n  Declared flows ({len(in_scope)}-task subset): "
        f"{passed}/{len(in_scope)} = {passed / len(in_scope):.4f}"
    )
    print(
        "  This is a different figure from the total above: the total is the standard "
        "τ²-bench Retail score, and neither stands in for the other."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute evaluation usage and cost")
    parser.add_argument("--run", default=None)
    parser.add_argument(
        "--project", type=int, default=None, help="project the cost of N tasks from per-task means"
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="print the average even when tasks are unscored (refused by default: "
        "that is not the run's score)",
    )
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    run_dir = (RESULTS_ROOT / args.run) if args.run else latest_run()
    simulations = load_simulations(run_dir)
    if not simulations:
        raise SystemExit(f"{run_dir} contains no simulation records")

    summary_path = run_dir / "summary.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    stack = summary.get("stack", {})
    agent_model = str(stack.get("agent_model", "unknown"))
    user_model = str(stack.get("user_simulator", FROZEN_USER_SIMULATOR))
    judge_model = str(stack.get("nl_judge", "unknown"))

    roles = token_breakdown(simulations)
    count = len(simulations)
    rewards = [
        float(sim["reward_info"]["reward"])
        for sim in simulations
        if (sim.get("reward_info") or {}).get("reward") is not None
    ]
    durations = [float(sim.get("duration") or 0) for sim in simulations]

    print(f"run directory: {run_dir.name}")
    print(f"model under test: {agent_model}    simulations: {count}")

    # With tasks missing, no average is printed by default. Upstream drops
    # infrastructure failures from the denominator, so losing 5 of 40 tasks yields 0.886
    # instead of 0.875: the loss raises no error and makes the score look better. A
    # better-looking number is far more dangerous than an error, so nothing is printed.
    expected = int(summary.get("results", {}).get("expected_count") or 0) or len(
        summary.get("task_ids") or []
    )
    missing = expected - len(rewards) if expected else 0
    if rewards and missing > 0 and not args.allow_incomplete:
        unscored = [
            str(sim.get("task_id"))
            for sim in simulations
            if (sim.get("reward_info") or {}).get("reward") is None
        ]
        print()
        print(
            f"[no score reported] expected {expected} tasks, only {len(rewards)} are scored, "
            f"{missing} missing."
        )
        print(f"           unscored: {sorted(set(unscored), key=lambda t: (len(t), t))}")
        print("           resume:   uv run python scripts/resume_run.py " + run_dir.name)
        print("           to see the average over what did run: add --allow-incomplete")
    elif rewards:
        avg = sum(rewards) / len(rewards)
        label = "average reward"
        if missing > 0:
            label = f"average reward (over the {len(rewards)} scored tasks only, not the run)"
        print(f"{label}: {avg:.4f}    passed: {sum(1 for r in rewards if r >= 1.0)}/{len(rewards)}")
    if durations:
        print(f"average duration: {sum(durations) / len(durations):.0f}s per task")

    print_scope_breakdown(simulations)

    print_usage_table(roles)

    agent = roles.get("assistant", RoleUsage())
    user = roles.get("user", RoleUsage())

    print(
        f"\nper task (agent side): input {agent.prompt / count:.0f} tok "
        f"({agent.cache_hit_rate:.0%} cache hits), output {agent.completion / count:.0f} tok"
    )
    print(f"agent cost: {format_cost(agent_model, agent)} (price snapshot {PRICE_SNAPSHOT_DATE})")

    # Cross-check against upstream litellm accounting. Two independent paths: litellm
    # sums per call, this script computes from total tokens. Registering CNY rather than
    # converting to USD is what makes exact equality possible here; a mismatch means one
    # of them is wrong, not that rounding differs.
    upstream = summary.get("results", {}).get("agent_cost_reported_by_upstream")
    ours = role_cost(agent_model, agent)
    if isinstance(upstream, int | float) and ours is not None:
        if upstream == 0:
            print(
                "    upstream litellm recorded 0: this run predates price registration, "
                "it was not free"
            )
        elif abs(upstream - ours) <= max(1e-6, ours * 1e-6):
            print(
                f"    matches upstream litellm accounting ({upstream:.6f} CNY); cache and "
                "reasoning conventions agree"
            )
        else:
            print(
                f"    [mismatch] upstream litellm says {upstream:.6f} CNY, this script "
                f"computes {ours:.6f} CNY. The two must be exactly equal; find out why "
                "before citing any cost figure"
            )
    print(f"simulated user: {format_cost(user_model, user)}")
    if user.reasoning and user.completion:
        print(
            f"    {user.reasoning / user.completion:.0%} of the simulated user's output is "
            "reasoning tokens, billed at the output rate, which was previously not counted"
        )
    judge_cost = print_judge_cost(summary, judge_model)

    # Total across the three roles. If any role cannot be computed, only a lower bound is
    # given and said so: adding an unavailable price as 0 is exactly the false accounting
    # this project's documentation warns about.
    parts: dict[str, float | None] = {
        "agent": role_cost(agent_model, agent),
        "simulated user": role_cost(user_model, user),
        "judge": judge_cost,
    }
    unknown = [name for name, value in parts.items() if value is None]
    total = sum(value for value in parts.values() if value is not None)
    share = "    ".join(f"{n} {v:.4f}" for n, v in parts.items() if v is not None)
    if unknown:
        print(f"\nthree-role total: >= {total:.4f} CNY ({share})")
        print(
            f"    no price available for {', '.join(unknown)}, so it is excluded; "
            "a missing price does not mean free."
        )
    else:
        print(f"\nthree-role total: {total:.4f} CNY ({share})    {total / count:.4f} CNY per task")

    if args.project and count:
        scale = args.project / count
        # The evaluation stack (simulated user plus judge) is independent of the model
        # under test, so it scales linearly with task count and can be added to each
        # candidate's cost.
        stack_cost = sum(
            value for name, value in parts.items() if name != "agent" and value is not None
        )
        print(
            f"\nprojection for {args.project} tasks "
            f"(at this run's measured cache hit rate of {agent.cache_hit_rate:.0%}):"
        )
        for model in sorted(DASHSCOPE_PRICES):
            projected = RoleUsage(
                prompt=int(agent.prompt * scale),
                completion=int(agent.completion * scale),
                cached=int(agent.cached * scale),
            )
            merged = role_cost(model, projected)
            tail = (
                f"    with the stack {merged + stack_cost * scale:.2f} CNY"
                if merged is not None and not unknown
                else ""
            )
            print(f"  {model:<26}{format_cost(model, projected)}{tail}")
        print(
            f"  evaluation stack (simulated user + judge) scaled linearly from this run: "
            f"{stack_cost * scale:.2f} CNY"
        )
        if unknown:
            print(
                f"  note: {', '.join(unknown)} has no price, so the stack figure is a lower bound."
            )
        print(
            "  Projected from this run's usage; models differ in verbosity and cache hit "
            "rate, which shifts the estimate."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
