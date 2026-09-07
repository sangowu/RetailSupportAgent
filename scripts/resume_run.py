"""补跑一轮里没拿到分数的题，结果合并进原运行目录。

存在的理由：服务端故障（额度、限流、超时）会让个别题跑不起来，而上游把这些题
**从分母里剔掉**——40 题丢 5 题会算出 0.886 而不是 0.875，丢题不报错，成绩还更好看。
`run_eval.py` 现在会把缺口记进 summary.json，`analyze_eval.py` 见到缺口拒绝报分，
本脚本负责把缺口补上。整轮重跑当然也行，但那要多花一整轮的钱。

**评测栈逐项核对**：模型、思考档位、判官、判官 JSON 格式任一与原轮不符就中止。
补跑的题必须和原轮跑在同一个栈上，否则合并回去就是掺假。

用法：
    uv run python scripts/resume_run.py <运行目录名>
    uv run python scripts/resume_run.py <运行目录名> --dry-run
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
    patch_nl_judge,
    prepare_environment,
)
from retail_support_agent.results_io import is_scored, load_simulations  # noqa: E402

RESULTS_ROOT = REPO / "evaluation" / "results"
DATA_DIR = REPO / "third_party" / "tau2-bench" / "data"

LINE_BREAK = chr(10)

# 原轮的**运行参数**：这些是调用方当时选的，补跑照抄即可，不存在"核对"一说。
REPLAYED_FIELDS = (
    "agent_model",
    "agent_thinking_enabled",
    "agent_thinking_effort",
    "echo_reasoning_content",
)

# 原轮的**环境解析结果**：这些不由调用方指定，而是从冻结常量、.env 和判官能力
# 推导出来的。它们才是会在两次运行之间悄悄漂移的东西——换了冻结模拟用户、
# 改了 RSA_JUDGE_MODEL、判官换型号导致 JSON 格式变了。
# 所以补跑时**不喂**这些值，让它们自然解析，再回头和原轮比对。
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
    """本轮没拿到分数的题。已被补跑修好的不再算在内（load_simulations 会去重）。"""
    return [str(sim.get("task_id")) for sim in load_simulations(run_dir) if not is_scored(sim)]


def rebuild_stack(settings: Settings, described: dict[str, Any]) -> EvalStack:
    """按原轮重建评测栈，并核对那些**不由我们指定**的部分。

    运行参数（REPLAYED_FIELDS）从 manifest 照抄——它们是输入，抄了就一定相等，
    再去"核对"是自欺欺人。真正要查的是 DERIVED_FIELDS：模拟用户、判官、判官的
    JSON 格式、三个端点，全都由冻结常量和 .env 推导而来，会在两次运行之间
    悄悄变化。所以这些**不喂进构造函数**，让它们照当前环境解析，再回头比对。

    密钥不参与比对，也不落盘。
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
        f"{key}: 原轮={described.get(key)!r} 现在={now.get(key)!r}"
        for key in DERIVED_FIELDS
        if key in described and now.get(key) != described.get(key)
    ]
    if drift:
        raise SystemExit(
            "评测栈与原轮不一致，拒绝补跑（合并回去会让这一轮的成绩变成两个栈的混合）："
            + LINE_BREAK
            + "  "
            + (LINE_BREAK + "  ").join(drift)
            + LINE_BREAK
            + "若确实要换栈，请整轮重跑，不要往旧目录里补。"
        )
    return stack


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="补跑一轮里未评分的题（会产生真实费用）")
    parser.add_argument("run", help="evaluation/results 下的运行目录名")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_dir = RESULTS_ROOT / args.run
    if not run_dir.is_dir():
        raise SystemExit(f"找不到运行目录：{run_dir}")
    summary: dict[str, Any] = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    missing = unscored_task_ids(run_dir)
    if not missing:
        print("本轮没有未评分的题，无需补跑。")
        return 0
    print(f"待补跑 {len(missing)} 题：{missing}")

    if args.dry_run:
        return 0

    prepare_environment(str(DATA_DIR))
    settings = Settings()  # type: ignore[call-arg]
    stack = rebuild_stack(settings, summary.get("stack", {}))
    print("评测栈与原轮逐项一致，开始补跑。")

    judge_usage = patch_nl_judge(stack)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    config = build_run_config(
        stack,
        task_ids=missing,
        num_trials=int(summary.get("num_trials", 1)),
        max_concurrency=args.concurrency,
        seed=int(summary.get("seed", 300) or 300),
        save_to=str(run_dir / f"resume_{stamp}.json"),
    )
    from tau2.run import run_domain

    run_domain(config)

    still = unscored_task_ids(run_dir)
    recovered = [t for t in missing if t not in still]
    # 补跑本身也要留痕：谁在什么时候补了哪几题，否则这一轮的结果无法解释
    (run_dir / f"resume_{stamp}.meta.json").write_text(
        json.dumps(
            {
                "resumed_at": datetime.now(UTC).isoformat(),
                "requested_task_ids": missing,
                "recovered_task_ids": recovered,
                "still_unscored_task_ids": still,
                "judge_usage": judge_usage.describe(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\n补回 {len(recovered)}/{len(missing)} 题。")
    if still:
        print(f"仍未评分：{still}——同样的故障还在，先排查再重试。", file=sys.stderr)
        return 1
    print("本轮已完整。重新统计：")
    print(f"  uv run python scripts/analyze_eval.py --run {run_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
