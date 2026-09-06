"""从评测原始结果统计用量与成本，并按此推算更大规模运行的花费。

上游用 litellm 记账，而 litellm 不知道 DashScope 的价格，`agent_cost` 一律是 0。
所以成本必须由我们自己按 token 用量乘价格快照算，并**注明取价日期**。

用法：
    uv run python scripts/analyze_eval.py                    # 分析最近一次运行
    uv run python scripts/analyze_eval.py --run <目录名>
    uv run python scripts/analyze_eval.py --project 74       # 按每题均值推算 74 题的花费
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RESULTS_ROOT = REPO / "evaluation" / "results"

# 价格快照：来源 https://help.aliyun.com/zh/model-studio/model-pricing
# 北京地域，元/百万 Token，核验日期 2026-09-06。
# qwen3.x-max 的数字文档标注为**非思考模式**，思考模式单价未取得。
PRICES: dict[str, tuple[float, float]] = {
    "qwen3.7-max-2026-06-08": (12.0, 36.0),
    "qwen3.8-max": (12.0, 36.0),
    "qwen3.8-flash": (0.8, 2.7),
    "deepseek-v4-pro-0813": (12.0, 24.0),
    "glm-5.2": (8.0, 28.0),
}
PRICE_SNAPSHOT_DATE = "2026-09-06"


def latest_run() -> Path:
    runs = sorted(p for p in RESULTS_ROOT.iterdir() if p.is_dir()) if RESULTS_ROOT.is_dir() else []
    if not runs:
        raise SystemExit("没有找到评测结果，先运行 scripts/run_eval.py")
    return runs[-1]


def load_results(run_dir: Path) -> dict[str, Any]:
    """上游把 save_to 当目录用，结果落在 simulations.json/results.json。"""
    for candidate in (run_dir / "simulations.json" / "results.json", run_dir / "results.json"):
        if candidate.is_file():
            data: dict[str, Any] = json.loads(candidate.read_text(encoding="utf-8"))
            return data
    raise SystemExit(f"{run_dir} 下找不到 results.json")


def token_breakdown(simulations: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """按消息角色拆分用量：assistant 是被测 Agent，user 是模拟用户。

    NL 断言评判的调用不在 messages 里，因此**统计不到**——报告必须说明这一点，
    不能把它当作"评判不花钱"。
    """
    agg: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for sim in simulations:
        for message in sim.get("messages") or []:
            usage = message.get("usage")
            if not usage:
                continue
            role = str(message.get("role", "unknown"))
            agg[role][0] += int(usage.get("prompt_tokens") or 0)
            agg[role][1] += int(usage.get("completion_tokens") or 0)
    return {role: (values[0], values[1]) for role, values in agg.items()}


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return prompt_tokens / 1e6 * price[0] + completion_tokens / 1e6 * price[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="统计评测用量与成本")
    parser.add_argument("--run", default=None)
    parser.add_argument("--project", type=int, default=None, help="按每题均值推算 N 题的花费")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    run_dir = (RESULTS_ROOT / args.run) if args.run else latest_run()
    data = load_results(run_dir)
    simulations: list[dict[str, Any]] = data.get("simulations") or []
    if not simulations:
        raise SystemExit(f"{run_dir} 里没有模拟记录")

    summary_path = run_dir / "summary.json"
    stack = {}
    if summary_path.is_file():
        stack = json.loads(summary_path.read_text(encoding="utf-8")).get("stack", {})
    agent_model = str(stack.get("agent_model", "unknown"))

    breakdown = token_breakdown(simulations)
    count = len(simulations)
    rewards = [
        float(sim["reward_info"]["reward"])
        for sim in simulations
        if (sim.get("reward_info") or {}).get("reward") is not None
    ]
    durations = [float(sim.get("duration") or 0) for sim in simulations]

    print(f"运行目录：{run_dir.name}")
    print(f"被测模型：{agent_model}    模拟数：{count}")
    if rewards:
        avg = sum(rewards) / len(rewards)
        full_marks = sum(1 for r in rewards if r >= 1.0)
        print(f"平均奖励：{avg:.4f}    满分题数：{full_marks}/{len(rewards)}")
    if durations:
        print(f"平均时长：{sum(durations) / len(durations):.0f} 秒/题")

    print(f"\n{'角色':<12}{'输入 tok':>12}{'输出 tok':>12}")
    for role, (prompt, completion) in sorted(breakdown.items()):
        print(f"{role:<12}{prompt:>12}{completion:>12}")

    agent_prompt, agent_completion = breakdown.get("assistant", (0, 0))
    agent_cost = cost_of(agent_model, agent_prompt, agent_completion)

    print(
        f"\n每题均值（被测 Agent 侧）："
        f"输入 {agent_prompt / count:.0f} tok，输出 {agent_completion / count:.0f} tok"
    )
    if agent_cost is None:
        print(f"成本：{agent_model} 不在价格快照里，无法折算")
        return 0

    per_task = agent_cost / count
    print(f"每题成本：{per_task:.4f} 元（价格快照 {PRICE_SNAPSHOT_DATE}）")
    print("未计入：模拟用户用量（价格未取得）、NL 评判调用（用量不在 messages 中）")

    if args.project:
        print(f"\n推算 {args.project} 题：")
        for model, (price_in, price_out) in sorted(PRICES.items()):
            projected = (
                agent_prompt / count / 1e6 * price_in + agent_completion / count / 1e6 * price_out
            ) * args.project
            print(f"  {model:<26}{projected:>9.2f} 元")
        print("  以上按参照模型的 token 用量推算；不同模型话多话少会带来偏差。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
