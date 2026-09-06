"""配对比较两次运行：McNemar 精确检验 + 成本/用量对比。

为什么必须配对：两次运行跑的是**同一批题**，题目难度是共享的。
按独立两组比平均分会把题目难度当成随机误差，白白丢掉大部分统计功效。
McNemar 只看"一边过、另一边没过"的那些题，正是我们想问的东西。

为什么用精确检验而不是卡方近似：不一致的题通常只有个位数，
卡方在这个量级上不可靠。

用法：
    uv run python scripts/compare_runs.py <基线目录名> <对比目录名>
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
    """题号 -> 奖励。未评分的模拟不计入，不当成 0。"""
    out: dict[str, float] = {}
    for sim in load_simulations(run_dir):
        info = sim.get("reward_info") or {}
        if info.get("reward") is not None:
            out[str(sim.get("task_id"))] = float(info["reward"])
    return out


def usage_of(run_dir: Path) -> dict[str, int]:
    """从 messages 里累计被测侧用量。判官用量另记在 summary.json。"""
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
    """双尾精确检验。b/c 是两个方向的不一致数；一致的题不携带信息。"""
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

    parser = argparse.ArgumentParser(description="两次运行的配对比较（McNemar 精确检验）")
    parser.add_argument("baseline", help="基线运行目录名")
    parser.add_argument("candidate", help="对比运行目录名")
    args = parser.parse_args()

    base_dir, cand_dir = RESULTS_ROOT / args.baseline, RESULTS_ROOT / args.candidate
    base, cand = rewards_by_task(base_dir), rewards_by_task(cand_dir)
    shared = sorted(set(base) & set(cand), key=int)
    if not shared:
        raise SystemExit("两次运行没有共同的已评分题目，无法配对比较")

    only_base = sorted(set(base) - set(cand), key=int)
    only_cand = sorted(set(cand) - set(base), key=int)
    if only_base or only_cand:
        print(f"[警告] 题集不完全重合，只比较共有的 {len(shared)} 题")
        print(f"    仅基线有：{only_base}    仅对比有：{only_cand}")

    facts = load_task_facts()
    base_sum, cand_sum = summary_of(base_dir), summary_of(cand_dir)
    base_model = str((base_sum.get("stack") or {}).get("agent_model", "?"))
    cand_model = str((cand_sum.get("stack") or {}).get("agent_model", "?"))

    def describe(label: str, run: dict[str, Any]) -> None:
        stack = run.get("stack") or {}
        print(
            f"  {label:<6} {stack.get('agent_model', '?'):<20}"
            f" 思考 {stack.get('agent_thinking_enabled', '?')}"
            f"/{stack.get('agent_thinking_effort', '未记录')}"
            f"  用户档位 {stack.get('user_simulator_thinking_effort', '未记录')}"
            f"  判官格式 {stack.get('nl_judge_response_format', '未记录')}"
        )

    print("=== 两次运行的配置 ===")
    describe("基线", base_sum)
    describe("对比", cand_sum)

    both = [t for t in shared if base[t] >= 1.0 and cand[t] >= 1.0]
    neither = [t for t in shared if base[t] < 1.0 and cand[t] < 1.0]
    lost = [t for t in shared if base[t] >= 1.0 and cand[t] < 1.0]
    gained = [t for t in shared if base[t] < 1.0 and cand[t] >= 1.0]

    print(f"\n=== 配对结果（共有 {len(shared)} 题）===")
    print(
        f"  基线 {len(both) + len(lost)}/{len(shared)}"
        f"    对比 {len(both) + len(gained)}/{len(shared)}"
    )
    print(f"  两边都过 {len(both)}    两边都没过 {len(neither)}")
    print(f"  对比修好了 {len(gained)} 题 {gained}")
    print(f"  对比弄坏了 {len(lost)} 题 {lost}")

    p = mcnemar_exact(len(lost), len(gained))
    print(f"\n  McNemar 精确检验 p = {p:.4f}", end="")
    if len(gained) + len(lost) == 0:
        print("    （没有任何一题结果不同，两者在这批题上无法区分）")
    elif p < 0.05:
        print("    **差异显著**")
    else:
        print(f"    未达显著——{len(gained) + len(lost)} 题的不一致量本就撑不起结论")

    judged = [t for t in shared if facts[t].invokes_judge]
    if judged:
        b_j = sum(1 for t in judged if base[t] >= 1.0)
        c_j = sum(1 for t in judged if cand[t] >= 1.0)
        print(f"\n  仅看真正调判官的 {len(judged)} 题：基线 {b_j}    对比 {c_j}")

    print("\n=== 被测侧用量与成本 ===")
    tier = deepseek_tier()
    for label, run_dir, model, run in (
        ("基线", base_dir, base_model, base_sum),
        ("对比", cand_dir, cand_model, cand_sum),
    ):
        u = usage_of(run_dir)
        cost = cost_of(model, u)
        judge = run.get("judge_usage") or {}
        shown = "价格快照里没有该模型" if cost is None else f"{cost:.3f} 元"
        print(
            f"  {label}  {u['calls']:>4} 次调用   输入 {u['prompt']:>8}"
            f"（缓存 {u['cached']:>7}）   输出 {u['completion']:>6}"
            f"（推理 {u['reasoning']:>6}）   {shown}"
        )
        if judge:
            print(
                f"        判官 {judge.get('judge_calls', 0)} 次，"
                f"输入 {judge.get('prompt_tokens', 0)}，输出 {judge.get('completion_tokens', 0)}"
            )
    print(f"\n  （DeepSeek 时段：{tier}；成本按 pricing.py 快照折算）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
