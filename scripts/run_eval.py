"""在 τ²-bench Retail 上跑评测。**会产生真实费用。**

先用极小样本试跑并估算成本，确认无误再放大——不要一上来就全量。

用法：
    uv run python scripts/run_eval.py --pilot                     # 2 题冒烟
    uv run python scripts/run_eval.py --split dev --limit 12      # 开发集抽样
    uv run python scripts/run_eval.py --split holdout             # 独立验证（不得用于调参）
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

from retail_support_agent.config import REFERENCE_MODEL, Settings  # noqa: E402
from retail_support_agent.evaluation import (  # noqa: E402
    EvalStack,
    build_run_config,
    patch_nl_judge,
    prepare_environment,
)
from retail_support_agent.tau2_pin import load_task_set  # noqa: E402

OUTPUT_ROOT = REPO / "evaluation" / "results"
DATA_DIR = REPO / "third_party" / "tau2-bench" / "data"


def select_tasks(split: str, limit: int | None, pilot: bool) -> list[str]:
    """任务一律取自 M0.1 冻结的清单，按固定顺序截取，保证可复现。"""
    if pilot:
        # 冒烟只用开发集最前面两题；开发集是唯一允许反复跑的集合
        pilot_ids: list[str] = load_task_set("dev")["task_ids"]
        return pilot_ids[:2]
    ids: list[str] = load_task_set(split)["task_ids"]
    return ids[:limit] if limit else ids


def summarize(results: Any) -> dict[str, Any]:
    """从上游结果里抽出报告需要的数字。不自己重算奖励。"""
    simulations = list(getattr(results, "simulations", []) or [])
    rewards: list[float] = []
    terminations: dict[str, int] = {}
    agent_cost = 0.0

    for sim in simulations:
        info = getattr(sim, "reward_info", None)
        if info is not None and getattr(info, "reward", None) is not None:
            rewards.append(float(info.reward))
        reason = str(getattr(sim, "termination_reason", "unknown"))
        terminations[reason] = terminations.get(reason, 0) + 1
        cost = getattr(sim, "agent_cost", None)
        if isinstance(cost, int | float):
            agent_cost += float(cost)
    return {
        "agent_cost_reported_by_upstream": agent_cost,
        "simulation_count": len(simulations),
        "scored_count": len(rewards),
        "avg_reward": (sum(rewards) / len(rewards)) if rewards else None,
        "pass_count": sum(1 for r in rewards if r >= 1.0),
        "termination_reasons": terminations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="τ²-bench Retail 评测（会产生真实费用）")
    parser.add_argument("--model", default=REFERENCE_MODEL, help="被测 Agent 模型")
    parser.add_argument("--split", default="dev", choices=["dev", "holdout", "regression"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pilot", action="store_true", help="只跑 2 题冒烟并估算成本")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--seed", type=int, default=300)
    parser.add_argument("--thinking", default=None, help="reasoning_effort 档位，默认关闭思考")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    task_ids = select_tasks(args.split, args.limit, args.pilot)
    if args.dry_run:
        print(
            f"将运行 {len(task_ids)} 题 × {args.trials} 次 = {len(task_ids) * args.trials} 个会话"
        )
        shown = ", ".join(task_ids[:10])
        more = " ..." if len(task_ids) > 10 else ""
        print(f"被测模型：{args.model}；任务：{shown}{more}")
        return 0

    prepare_environment(str(DATA_DIR))
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"配置读取失败（检查 .env）：{type(exc).__name__}", file=sys.stderr)
        return 2

    # 思考配置：M0.2 已验证互斥规则不可依赖，这里只传一个参数
    agent_extra: dict[str, Any] = {"extra_body": {"enable_thinking": args.thinking is not None}}
    if args.thinking:
        agent_extra["extra_body"]["reasoning_effort"] = args.thinking

    stack = EvalStack(settings=settings, agent_model=args.model, agent_extra=agent_extra)
    patch_nl_judge(stack)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUTPUT_ROOT / f"{run_id}_{args.model.replace('/', '_')}_{args.split}"
    run_dir.mkdir(parents=True, exist_ok=True)

    config = build_run_config(
        stack,
        task_ids=task_ids,
        num_trials=args.trials,
        max_concurrency=args.concurrency,
        seed=args.seed,
        save_to=str(run_dir / "simulations.json"),
    )

    print(f"运行 {len(task_ids)} 题 × {args.trials} 次，被测模型 {args.model}", flush=True)
    started = time.monotonic()
    # 注意：tau2.run.run_tasks 是旧版兼容 shim，与 runner.batch.run_tasks 同名但签名不同。
    # run_domain 是上游文档指定的高层入口，会自己按 config 里的 task_ids 过滤并算指标。
    from tau2.run import run_domain

    results = run_domain(config)
    elapsed = time.monotonic() - started

    summary = {
        "run_id": run_id,
        "run_at": datetime.now(UTC).isoformat(),
        "wall_seconds": round(elapsed, 1),
        "task_ids": task_ids,
        "num_trials": args.trials,
        "stack": stack.describe(),
        "results": summarize(results),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary["results"], ensure_ascii=False, indent=2))
    print(f"\n原始结果：{run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
