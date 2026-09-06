"""用另一个判官对**已保存的轨迹**重新判定 NL 断言，量化判官噪声。

为什么值得单独做：上游对断言是全有全无——
`reward = 1.0 if all(met) else 0.0`——判官在任意一条断言上抖一下，
整题就从满分掉到零分。噪声不会被平均掉，只会被放大。

关键性质：**不重跑 Agent**。判官只吃"对话 + 断言"，两者都已存在结果文件里，
所以复判只花判官那一侧的 token，是整个项目里最便宜的一次可信度检查。

用法：
    uv run python scripts/recheck_judge.py --run <目录名>            # 用当前配置的判官复判
    uv run python scripts/recheck_judge.py --run <目录名> --repeat 3 # 同一判官跑三遍看自洽性
    uv run python scripts/recheck_judge.py --list                    # 只看哪些运行含可复判的题
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
    """断言文本取自上游任务文件，而不是结果文件——结果文件里可能是空的。"""
    tasks_path = REPO / "third_party" / "tau2-bench" / "data" / "tau2" / "domains" / "retail"
    raw: Any = json.loads((tasks_path / "tasks.json").read_text(encoding="utf-8"))
    tasks = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    for task in tasks:
        if str(task.get("id")) == task_id:
            criteria = task.get("evaluation_criteria") or {}
            return list(criteria.get("nl_assertions") or [])
    return []


def trajectory_of(sim: dict[str, Any]) -> list[Any]:
    """判官只用到 role 与 content，用轻量替身即可，不必反序列化整个 Message。"""
    return [
        SimpleNamespace(role=str(m.get("role")), content=m.get("content"))
        for m in (sim.get("messages") or [])
    ]


def stored_verdicts(sim: dict[str, Any]) -> dict[str, bool]:
    checks = (sim.get("reward_info") or {}).get("nl_assertions") or []
    return {str(c.get("nl_assertion")): bool(c.get("met")) for c in checks}


def main() -> int:
    # 必须在 parse_args 之前：--help 走 stdout，GBK 控制台遇中文会抛 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="离线复判 NL 断言（只花判官侧 token）")
    parser.add_argument("--run", default=None, help="evaluation/results 下的运行目录名")
    parser.add_argument("--repeat", type=int, default=1, help="同一判官重复判定次数，看自洽性")
    parser.add_argument("--list", action="store_true", help="列出各运行里可复判的题数后退出")
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
            print(f"{run_dir.name:<50} 模拟 {len(sims):>3}    可复判 {judgeable:>3}")
        return 0

    if not args.run:
        raise SystemExit("需要 --run <目录名>，或用 --list 先看有哪些运行")

    run_dir = RESULTS_ROOT / args.run
    sims = load_simulations(run_dir)
    targets = [
        s for s in sims if (tid := str(s.get("task_id"))) in facts and facts[tid].invokes_judge
    ]
    if not targets:
        print(f"{args.run} 里没有任何带 NL 断言的题——这次运行从未调用过判官，无可复判。")
        print("请先在带断言的题上跑一次评测（scripts/run_eval.py --pilot 已改为只选带断言的题）。")
        return 1

    prepare_environment(str(DATA_DIR))
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"配置读取失败（检查 .env）：{type(exc).__name__}", file=sys.stderr)
        return 2

    # agent_model 在复判里不发任何请求，只为构造合法的 EvalStack。
    stack = EvalStack(settings=settings, agent_model="unused-in-recheck")
    judge_usage = patch_nl_judge(stack)
    for warning in stack.warnings():
        print(f"[警告] {warning}")

    from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator

    print(f"复判判官：{stack.judge_model}    独立端点：{stack.judge_is_independent}")
    print(f"可复判题数：{len(targets)}    每题判定 {args.repeat} 次\n")

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
            # 上游拿到判官回复直接 json.loads，没有容错：判官吐出非 JSON 就整轮崩。
            # 这里把"判官坏了"当成一条要记录的观测，而不是让它毁掉整次复判——
            # 一个产不出可解析输出的判官，本身就是必须报告的结论。
            try:
                checks = NLAssertionsEvaluator.evaluate_nl_assertions(trajectory, assertions)
            except json.JSONDecodeError:
                broken += 1
                continue
            rounds.append({c.nl_assertion: bool(c.met) for c in checks})
        if broken:
            print(f"任务 {task_id:<5} 判官 {broken}/{args.repeat} 次返回非 JSON，无法解析")
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

        # 自洽性：同一判官多轮之间是否给出同样结论
        per_assertion_stable = {
            a: len({r.get(a) for r in rounds}) == 1 for a in {k for r in rounds for k in r}
        }
        task_stable = all(per_assertion_stable.values())
        unstable += 0 if task_stable else 1

        # 与原判官的分歧
        first = rounds[0]
        diffs = [a for a, met in first.items() if a in previous and previous[a] != met]
        disagreements += len(diffs)

        old_reward = 1.0 if previous and all(previous.values()) else (1.0 if not previous else 0.0)
        new_reward = 1.0 if all(first.values()) else 0.0

        flag = ""
        if not task_stable:
            flag = "  ← 同一判官自相矛盾"
        elif diffs:
            flag = "  ← 与原判官分歧"
        print(
            f"任务 {task_id:<5} 断言 {len(assertions)}    "
            f"原判 {old_reward:.0f} → 复判 {new_reward:.0f}    分歧 {len(diffs)}{flag}"
        )
        for assertion in diffs:
            print(f"    原判 {previous[assertion]} / 复判 {first[assertion]}：{assertion[:80]}")

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
            f"\n[严重] 判官共 {unparseable} 次返回非 JSON——上游对此没有容错，"
            f"真跑评测时会直接把整轮跑崩。这个判官不可用于正式运行。"
        )
    print(f"\n合计断言 {total_assertions} 条，与原判官分歧 {disagreements} 条")
    if args.repeat > 1:
        print(f"同一判官自相矛盾的题数：{unstable}/{len(records)}")
    print("提示：分歧多不代表新判官更差，只代表两者不可互换——历史成绩必须整体重判。")

    # 这次复判到底花了多少：判官是唯一发请求的一方，Agent 侧一分钱没花
    usage = judge_usage.describe()
    tier = deepseek_tier()
    price = price_for(str(stack.judge_model))
    cost = (
        price.cost(usage["prompt_tokens"], usage["completion_tokens"], usage["cached_tokens"])
        if price
        else None
    )
    print(
        f"\n判官用量：{usage['judge_calls']} 次调用，"
        f"输入 {usage['prompt_tokens']} tok（缓存命中 {usage['cached_tokens']}），"
        f"输出 {usage['completion_tokens']} tok（其中推理 {usage['reasoning_tokens']}）"
    )
    if cost is None:
        print(f"本次复判花费：{stack.judge_model} 不在价格快照里，无法折算")
    else:
        print(f"本次复判花费：约 {cost:.4f} 元（{tier} 时段价；Agent 侧零成本）")

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
    )
    print(f"明细：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
