"""从评测原始结果统计用量与成本，并按此推算更大规模运行的花费。

上游用 litellm 记账，而 litellm 不知道 DashScope 与 DeepSeek 的价格，
`agent_cost` 一律是 0。所以成本必须由我们自己按 token 用量乘价格快照算。

三笔此前漏掉、且实测证明会改变结论的账：

1. **缓存命中**：被测侧 72% 的输入 token 命中隐式缓存，命中部分按输入价 20% 计费，
   按全价折算会**高估**成本。
2. **推理 token**：模拟用户 89% 的输出 token 是 reasoning，按输出价计费，此前没算。
3. **判官**：调用不在 messages 里，须由 JudgeUsageRecorder 单独记账。

用量取自 `message.raw_data.usage`——上游的 `usage` 字段只留了两个数，
但完整响应一直存在 raw_data 里，所以历史结果也能回溯。

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
    """一个角色（被测 Agent / 模拟用户）的累计用量。"""

    calls: int = 0
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    reasoning: int = 0
    missing_usage: int = 0
    # 服务端**根本没报**缓存字段的调用数。与"报了、是 0"必须分开：
    # 前者是未知（我们按全价计，偏保守），后者才是确认没命中。
    # 实测 qwen3.6-plus 的 prompt_tokens_details.cached_tokens 恒为 null，
    # 混作一谈会让报告里的"命中率 0%"被读成"这个模型没有缓存收益"。
    cache_unreported: int = 0

    @property
    def cache_hit_rate(self) -> float:
        return self.cached / self.prompt if self.prompt else 0.0

    @property
    def cache_is_unknown(self) -> bool:
        """全部调用都没报缓存字段——命中率无从得知，不是 0。"""
        return self.calls > 0 and self.cache_unreported == self.calls


def latest_run() -> Path:
    runs = sorted(p for p in RESULTS_ROOT.iterdir() if p.is_dir()) if RESULTS_ROOT.is_dir() else []
    if not runs:
        raise SystemExit("没有找到评测结果，先运行 scripts/run_eval.py")
    return runs[-1]


def _usage_of(message: dict[str, Any]) -> dict[str, Any] | None:
    """优先用 raw_data 里的完整响应；它带缓存与推理明细，上游的 usage 字段没有。"""
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
    """缓存命中数；服务端没报该字段时返回 None（而不是 0）。

    三种字段名都可能出现：我们补丁后的扁平字段、OpenAI 的嵌套明细、
    DeepSeek 官方端点的 prompt_cache_hit_tokens。全都缺才算"未报"。
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
    """按消息角色拆分用量：assistant 是被测 Agent，user 是模拟用户。

    NL 断言评判的调用**不在 messages 里**，这里统计不到；
    它由 JudgeUsageRecorder 在运行时单独记账，写进 summary.json。
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
    """缓存单价未知的模型只给区间，不给一个假装精确的数字。"""
    price = price_for(model)
    if price is None:
        return f"{model} 不在价格快照里，无法折算"
    high = price.cost(usage.prompt, usage.completion, usage.cached)
    if cache_ratio_is_known(model) or not usage.cached:
        return f"{high:.4f} 元"
    low = price.cost_floor(usage.prompt, usage.completion, usage.cached)
    return f"{low:.4f} – {high:.4f} 元（缓存命中单价未公开，只能给区间）"


def print_usage_table(roles: dict[str, RoleUsage]) -> None:
    print(
        f"\n{'角色':<10}{'调用':>6}{'输入':>10}{'其中缓存':>10}{'命中率':>8}{'输出':>9}{'其中推理':>10}"
    )
    for role, usage in sorted(roles.items()):
        print(
            f"{role:<10}{usage.calls:>6}{usage.prompt:>10}{usage.cached:>10}"
            f"{usage.cache_hit_rate:>7.0%}{usage.completion:>9}{usage.reasoning:>10}"
        )
        if usage.missing_usage:
            print(
                f"{'':<10}（另有 {usage.missing_usage} 条消息没有用量记录，未计入——"
                f"通常是开场白等本地生成、未调用模型的消息）"
            )
        if usage.cache_unreported:
            scope = "全部" if usage.cache_is_unknown else f"{usage.cache_unreported} 次"
            print(
                f"{'':<10}（{scope}调用服务端未返回缓存字段：命中率**未知**，非 0；"
                f"按全价折算，偏保守）"
            )


def role_cost(model: str, usage: RoleUsage) -> float | None:
    """该角色的成本；模型不在价格快照里返回 None——**不能当成 0**。"""
    price = price_for(model)
    if price is None:
        return None
    return price.cost(usage.prompt, usage.completion, usage.cached)


def print_judge_cost(summary: dict[str, Any], judge_model: str) -> float | None:
    judge_usage = summary.get("judge_usage")
    if not judge_usage:
        print(f"NL 判官（{judge_model}）：本次运行未记录用量——该运行早于判官记账功能。")
        print("    注意：统计不到不等于没花钱，这笔账此前一直是空白。")
        return None
    prompt = int(judge_usage.get("prompt_tokens", 0))
    completion = int(judge_usage.get("completion_tokens", 0))
    cached = int(judge_usage.get("cached_tokens", 0))
    price = price_for(judge_model)
    cost = price.cost(prompt, completion, cached) if price else None
    tail = f"约 {cost:.4f} 元" if cost is not None else "价格未取得"
    print(
        f"NL 判官（{judge_model}）：{judge_usage.get('judge_calls', 0)} 次调用，"
        f"输入 {prompt} tok（缓存 {cached}），输出 {completion} tok，{tail}"
    )
    return cost


def main() -> int:
    parser = argparse.ArgumentParser(description="统计评测用量与成本")
    parser.add_argument("--run", default=None)
    parser.add_argument("--project", type=int, default=None, help="按每题均值推算 N 题的花费")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="本轮有题未评分时，仍然打印平均分（默认拒绝：那不是本轮成绩）",
    )
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    run_dir = (RESULTS_ROOT / args.run) if args.run else latest_run()
    simulations = load_simulations(run_dir)
    if not simulations:
        raise SystemExit(f"{run_dir} 里没有模拟记录")

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

    print(f"运行目录：{run_dir.name}")
    print(f"被测模型：{agent_model}    模拟数：{count}")

    # 缺题时**默认不报平均分**。上游把服务端故障的题从分母里剔掉，
    # 40 题丢 5 题会算出 0.886 而不是 0.875——丢题不报错，还让成绩更好看。
    # 一个更好看的数字比一个报错危险得多，所以这里宁可什么都不给。
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
        print(f"[拒绝报分] 应有 {expected} 题，只有 {len(rewards)} 题拿到分数，缺 {missing} 题。")
        print(f"           未评分：{sorted(set(unscored), key=lambda t: (len(t), t))}")
        print("           补跑：uv run python scripts/resume_run.py " + run_dir.name)
        print("           只想看跑通那部分的平均分：加 --allow-incomplete")
    elif rewards:
        avg = sum(rewards) / len(rewards)
        label = "平均奖励"
        if missing > 0:
            label = f"平均奖励（**仅跑通的 {len(rewards)} 题**，非本轮成绩）"
        print(
            f"{label}：{avg:.4f}    满分题数：{sum(1 for r in rewards if r >= 1.0)}/{len(rewards)}"
        )
    if durations:
        print(f"平均时长：{sum(durations) / len(durations):.0f} 秒/题")

    print_usage_table(roles)

    agent = roles.get("assistant", RoleUsage())
    user = roles.get("user", RoleUsage())

    print(
        f"\n每题均值（被测 Agent 侧）："
        f"输入 {agent.prompt / count:.0f} tok（缓存命中 {agent.cache_hit_rate:.0%}），"
        f"输出 {agent.completion / count:.0f} tok"
    )
    print(f"被测成本：{format_cost(agent_model, agent)}（价格快照 {PRICE_SNAPSHOT_DATE}）")
    print(f"模拟用户：{format_cost(user_model, user)}")
    if user.reasoning and user.completion:
        print(
            f"    模拟用户 {user.reasoning / user.completion:.0%} 的输出是推理 token，"
            f"按输出价计费——此前完全没算这笔"
        )
    judge_cost = print_judge_cost(summary, judge_model)

    # 三个角色的合计。任何一个角色算不出来，合计就只能给下界并明说——
    # 把「价格未取得」当成 0 累加，正是本项目文档反复警告的那种假账。
    parts: dict[str, float | None] = {
        "被测": role_cost(agent_model, agent),
        "模拟用户": role_cost(user_model, user),
        "判官": judge_cost,
    }
    unknown = [name for name, value in parts.items() if value is None]
    total = sum(value for value in parts.values() if value is not None)
    share = "    ".join(f"{n} {v:.4f}" for n, v in parts.items() if v is not None)
    if unknown:
        print(f"\n三角色合计：≥ {total:.4f} 元（{share}）")
        print(f"    {'、'.join(unknown)} 的单价未取得，**未计入**——缺价不等于免费。")
    else:
        print(f"\n三角色合计：{total:.4f} 元（{share}）    每题 {total / count:.4f} 元")

    if args.project and count:
        scale = args.project / count
        # 评测栈（模拟用户 + 判官）与被测模型无关，换被测模型这部分不变，
        # 所以可以按题数线性放大后加到每个候选的成本上。
        stack_cost = sum(
            value for name, value in parts.items() if name != "被测" and value is not None
        )
        print(f"\n推算 {args.project} 题（按本次实测的缓存命中率 {agent.cache_hit_rate:.0%}）：")
        for model in sorted(DASHSCOPE_PRICES):
            projected = RoleUsage(
                prompt=int(agent.prompt * scale),
                completion=int(agent.completion * scale),
                cached=int(agent.cached * scale),
            )
            merged = role_cost(model, projected)
            tail = (
                f"    含评测栈 {merged + stack_cost * scale:.2f} 元"
                if merged is not None and not unknown
                else ""
            )
            print(f"  {model:<26}{format_cost(model, projected)}{tail}")
        print(f"  评测栈（模拟用户 + 判官）按本次实测线性放大：{stack_cost * scale:.2f} 元")
        if unknown:
            print(f"  注意：{'、'.join(unknown)} 缺价，评测栈这笔是**下界**。")
        print("  按本次用量推算；不同模型话多话少、缓存命中率不同都会带来偏差。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
