"""在 τ²-bench Retail 上跑评测。**会产生真实费用。**

先用极小样本试跑并估算成本，确认无误再放大——不要一上来就全量。

用法：
    uv run python scripts/run_eval.py --pilot                     # 2 题冒烟（必含 NL 断言）
    uv run python scripts/run_eval.py --split dev --sample 6      # 按判官覆盖比例抽 6 题
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

from retail_support_agent.config import SELECTED_MODEL, Settings  # noqa: E402
from retail_support_agent.evaluation import (  # noqa: E402
    EvalStack,
    EvaluationSetupError,
    build_run_config,
    patch_nl_judge,
    prepare_environment,
)
from retail_support_agent.llm import THINKING_EFFORT, VALID_EFFORTS  # noqa: E402
from retail_support_agent.task_meta import load_task_facts, split_composition  # noqa: E402
from retail_support_agent.tau2_pin import load_task_set  # noqa: E402

OUTPUT_ROOT = REPO / "evaluation" / "results"
DATA_DIR = REPO / "third_party" / "tau2-bench" / "data"


def select_tasks(split: str, limit: int | None, sample: int | None, pilot: bool) -> list[str]:
    """任务一律取自 M0.1 冻结的清单，按固定顺序截取，保证可复现。

    `--limit` 的朴素截断有个已被实测证伪的隐患：开发集最前面两题
    **都没有 NL 断言**，上游对空断言列表直接给 1.0，判官根本不会被调用。
    第一次冒烟就栽在这里——2/2 满分里那半个 NL_ASSERTION 是空过的。
    所以冒烟改为只挑真正会调判官的题，抽样则按判官覆盖比例分层。
    """
    facts = load_task_facts()
    ids: list[str] = load_task_set(split)["task_ids"]

    if pilot:
        # 开发集是唯一允许反复跑的集合；只取会真正走判官的题
        judge_ids = [t for t in load_task_set("dev")["task_ids"] if facts[t].invokes_judge]
        if len(judge_ids) < 2:
            raise SystemExit("开发集里带 NL 断言的题不足 2 道，无法冒烟")
        return judge_ids[:2]

    if sample:
        # 保持该划分原有的"带断言 / 不带断言"比例，且顺序固定、可复现
        judge_ids = [t for t in ids if facts[t].invokes_judge]
        other_ids = [t for t in ids if not facts[t].invokes_judge]
        n_judge = max(1, round(sample * len(judge_ids) / len(ids))) if judge_ids else 0
        n_judge = min(n_judge, len(judge_ids), sample)
        picked = set(judge_ids[:n_judge]) | set(other_ids[: sample - n_judge])
        return [t for t in ids if t in picked]  # 回到冻结清单的原始顺序

    return ids[:limit] if limit else ids


def plan_phases(task_ids: list[str], early_stop: int | None) -> list[list[str]]:
    """把题目切成"先试探"与"跑完剩下"两段。

    一轮定模型意味着没有后续档位兜底，但也没必要给一个明显不合格的配置
    把全部题目跑完——先跑前 N 题，全零就掐掉。N 不合法或够不着就退回单段。
    """
    if not early_stop or early_stop <= 0 or early_stop >= len(task_ids):
        return [task_ids]
    return [task_ids[:early_stop], task_ids[early_stop:]]


def average_reward(simulations: list[Any]) -> float | None:
    """已完成模拟的平均奖励；没有任何可评分的记录时返回 None。"""
    rewards = [
        float(info.reward)
        for sim in simulations
        if (info := getattr(sim, "reward_info", None)) is not None
        and getattr(info, "reward", None) is not None
    ]
    return sum(rewards) / len(rewards) if rewards else None


def summarize(simulations: list[Any], expected: int) -> dict[str, Any]:
    """从上游结果里抽出报告需要的数字。不自己重算奖励。

    `expected` 是本应跑完的会话数（题数 × 轮数）。传它进来是为了**对账**：
    上游把服务端故障的题从分母里剔掉，40 题丢 5 题会算出 0.886 而不是 0.875——
    丢题不报错，还让成绩更好看。所以缺口必须被显式记下来，
    而不是等着谁去翻日志才发现。
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
        # 缺口不为零时，avg_reward 只是"跑通那部分的平均分"，不是本轮成绩。
        # analyze_eval 读到 incomplete=True 会拒绝直接报它。
        "incomplete": incomplete,
        "unscored_task_ids": sorted(set(unscored), key=lambda t: (len(t), t)),
        "avg_reward": (sum(rewards) / len(rewards)) if rewards else None,
        "pass_count": sum(1 for r in rewards if r >= 1.0),
        "termination_reasons": terminations,
    }


def main() -> int:
    # 必须在 parse_args 之前：argparse 的 --help 直接往 stdout 写，
    # Windows 默认 GBK 控制台遇到中文帮助文本会直接抛 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="τ²-bench Retail 评测（会产生真实费用）")
    parser.add_argument("--model", default=SELECTED_MODEL, help="被测 Agent 模型")
    parser.add_argument("--split", default="dev", choices=["dev", "holdout", "regression"])
    parser.add_argument("--limit", type=int, default=None, help="朴素截断前 N 题")
    parser.add_argument("--sample", type=int, default=None, help="按判官覆盖比例分层抽 N 题")
    parser.add_argument("--pilot", action="store_true", help="只跑 2 题冒烟（必含 NL 断言）")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--seed", type=int, default=300)
    parser.add_argument(
        "--thinking",
        default=THINKING_EFFORT,
        choices=sorted(VALID_EFFORTS),
        help=f"被测 Agent 的 reasoning_effort 档位，默认 {THINKING_EFFORT}",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="关闭被测 Agent 的思考（DeepSeek 默认开启，必须显式关）",
    )
    parser.add_argument(
        "--early-stop",
        type=int,
        default=None,
        metavar="N",
        help="先跑前 N 题；平均奖励不超过阈值就掐掉，不跑剩下的",
    )
    parser.add_argument(
        "--early-stop-below",
        type=float,
        default=0.001,
        help="提前终止阈值，默认 0.001（即前 N 题全零才掐）",
    )
    parser.add_argument(
        "--no-reasoning-echo",
        action="store_true",
        help="不回传历史 reasoning_content（消融用；DeepSeek 文档要求带 tools 时回传）",
    )
    parser.add_argument(
        "--user-no-thinking",
        action="store_true",
        help=(
            "关闭模拟用户的思考（对照实验用；会使成绩与既有运行不可比）。"
            "对当前冻结的 glm-5.3-flash **不可用**：智谱 GLM-5.3 系列强制思考，"
            "会在启动时报错并提示改用旧模拟用户"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    task_ids = select_tasks(args.split, args.limit, args.sample, args.pilot)
    if args.dry_run:
        print(
            f"将运行 {len(task_ids)} 题 × {args.trials} 次 = {len(task_ids) * args.trials} 个会话"
        )
        shown = ", ".join(task_ids[:10])
        more = " ..." if len(task_ids) > 10 else ""
        print(f"被测模型：{args.model}；任务：{shown}{more}")
        facts = load_task_facts()
        judged = [t for t in task_ids if facts[t].invokes_judge]
        print(f"其中会真正调用 NL 判官的：{len(judged)}/{len(task_ids)} 题 -> {judged}")
        print(f"该划分整体构成：{split_composition(args.split)}")
        return 0

    prepare_environment(str(DATA_DIR))
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"配置读取失败（检查 .env）：{type(exc).__name__}", file=sys.stderr)
        return 2
    # 注意：异常信息一律不打印内容，只打印类型——它可能带上环境变量的值。

    # 思考配置由 EvalStack 按端点翻译成对应方言：
    # DeepSeek 是 thinking.{type,reasoning_effort}，
    # DashScope 是 enable_thinking + reasoning_effort。
    # 发错方言不会报错，只会被静默忽略——所以绝不在这里手搓 extra_body。
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
        # 这些是配置错误，不是崩溃：栈回溯只会淹没真正有用的那句话。
        # 消息本身只含端点与型号，不含密钥。
        print(f"评测栈配置有误：{exc}", file=sys.stderr)
        return 2
    if args.user_no_thinking:
        print(
            "[注意] 模拟用户已关思考：这改变了冻结评测栈，本次成绩与既有运行不可比。",
            flush=True,
        )
    # 返回的记录器负责给判官记账：它的调用不在 messages 里，不主动记就永远统计不到
    judge_usage = patch_nl_judge(stack)
    for warning in stack.warnings():
        print(f"[警告] {warning}", flush=True)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = OUTPUT_ROOT / f"{run_id}_{args.model.replace('/', '_')}_{args.split}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 提前终止：先跑前 N 题，明显不合格就不再往下烧钱。
    # 分段落盘，读的时候由 results_io 合并回一次运行。
    phases = plan_phases(task_ids, args.early_stop)

    thinking_note = "关闭" if args.no_thinking else f"开启 effort={args.thinking}"
    print(
        f"运行 {len(task_ids)} 题 × {args.trials} 次，被测模型 {args.model}（思考{thinking_note}）",
        flush=True,
    )
    if len(phases) > 1:
        print(
            f"提前终止已开启：先跑 {len(phases[0])} 题，平均奖励 ≤ {args.early_stop_below} 就停",
            flush=True,
        )

    # 注意：tau2.run.run_tasks 是旧版兼容 shim，与 runner.batch.run_tasks 同名但签名不同。
    # run_domain 是上游文档指定的高层入口，会自己按 config 里的 task_ids 过滤并算指标。
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
        )
        if len(phases) > 1:
            print(f"\n第 {index}/{len(phases)} 段：{len(phase_ids)} 题", flush=True)
        results = run_domain(config)
        all_simulations.extend(getattr(results, "simulations", []) or [])
        completed_ids.extend(phase_ids)

        if index < len(phases):
            avg = average_reward(all_simulations)
            if avg is not None and avg <= args.early_stop_below:
                stopped_early = True
                print(
                    f"\n提前终止：前 {len(completed_ids)} 题平均奖励 {avg:.4f}"
                    f" ≤ {args.early_stop_below}，剩余 {len(task_ids) - len(completed_ids)}"
                    f" 题不再运行。",
                    flush=True,
                )
                print("注意：这是「明显不合格」的判定，不是排名结论。", flush=True)
                break
            shown = "无可评分记录" if avg is None else f"{avg:.4f}"
            print(f"前 {len(completed_ids)} 题平均奖励 {shown}，继续。", flush=True)

    elapsed = time.monotonic() - started

    summary = {
        "run_id": run_id,
        "run_at": datetime.now(UTC).isoformat(),
        "wall_seconds": round(elapsed, 1),
        "task_ids": task_ids,
        "completed_task_ids": completed_ids,
        "stopped_early": stopped_early,
        "num_trials": args.trials,
        "stack": stack.describe(),
        "judge_usage": judge_usage.describe(),
        "results": summarize(all_simulations, expected=len(completed_ids) * args.trials),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary["results"], ensure_ascii=False, indent=2))
    results = summary["results"]
    if results["incomplete"]:
        missing = results["expected_count"] - results["scored_count"]
        print(
            f"\n[严重] 本轮缺 {missing} 个会话未评分，"
            f"上面的 avg_reward={results['avg_reward']} 是**剩下那部分**的平均分，不是本轮成绩。",
            file=sys.stderr,
        )
        print(f"       未评分题：{results['unscored_task_ids']}", file=sys.stderr)
        print(f"       终止原因：{results['termination_reasons']}", file=sys.stderr)
        print(
            "       补跑：uv run python scripts/resume_run.py "
            f"{run_dir.name}（评测栈会逐项核对，不一致则拒绝）",
            file=sys.stderr,
        )
    print(f"判官用量：{judge_usage.describe()}")
    print(f"\n原始结果：{run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
