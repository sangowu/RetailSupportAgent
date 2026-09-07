"""离线审查**模拟用户**的行为质量。换模拟用户时，这是唯一站得住的"更好"口径。

为什么不能用 Agent 分数衡量模拟用户：分数高可能恰恰因为模拟用户变差了。
2026-09-06 实测：关掉模拟用户的思考后 Agent 分数从 18/24 涨到 23/24——
原因是它把一道复合任务拆成了几道单任务，把考卷改简单了。
上游把这类错误明确归为 `critical_helped`（帮了 Agent 不该帮的忙）。

用上游的 `UserOnlyReviewer`：它逐条核对模拟用户说过的每个事实是否能从
任务指令里推出来，并分级：
  critical_helped   —— 让任务变简单（泄露信息、代替 Agent 解题）
  critical_hindered —— 让任务变难或不可能（说了错话、自相矛盾）
  minor             —— 有错但没影响结果

**审查模型必须跨候选保持同一个**，否则比的是审查模型不是模拟用户。

用法：
    uv run python scripts/review_user_sim.py --run <目录名>
    uv run python scripts/review_user_sim.py --run <A> --run <B>   # 并排比较
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

# 审查模型：与被测（deepseek-v4）、判官（qwen3.7-flash）、以及正在比较的两个
# 模拟用户（qwen3.6-plus / glm-5.3-flash）都不同源，避免"自家人给自家人打分"。
# 一旦选定就不要换——换了就不是在比模拟用户了。
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
        # 上游没把 user_info 落盘（`sim["info"]` 是空的），所以自己构造：
        # guidelines 取自数据目录里的同一份文件，与运行时用的是同一段文本。
        # retail 域的模拟用户不使用工具，故 use_tools=False。
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

    parser = argparse.ArgumentParser(description="离线审查模拟用户的行为质量")
    parser.add_argument("--run", action="append", required=True, help="可重复，用于并排比较")
    parser.add_argument("--reviewer", default=DEFAULT_REVIEWER)
    args = parser.parse_args()

    prepare_environment(str(DATA_DIR))
    capture_full_usage()
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"配置读取失败（检查 .env）：{type(exc).__name__}", file=sys.stderr)
        return 2

    # 审查判官跑在 DashScope 上：它既不是被测、也不是模拟用户候选，额度互不影响。
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
    print(f"审查模型：{args.reviewer}（跨候选必须保持同一个）\n")

    results = []
    for name in args.run:
        run_dir = RESULTS_ROOT / name
        result = review_run(run_dir, args.reviewer, tasks)
        results.append(result)
        out = run_dir / f"user_sim_review_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"{result['user_simulator']:<26} 明细 -> {out.name}")

    print(f"\n{'模拟用户':<26}{'已审':>6}{'有错':>6}{'严重':>6}   分级")
    for r in results:
        by = r["errors_by_severity"]
        detail = "  ".join(f"{k} {v}" for k, v in sorted(by.items())) or "无"
        print(
            f"{r['user_simulator']:<26}{r['reviewed']:>6}"
            f"{r['sims_with_any_error']:>6}{r['sims_with_critical_error']:>6}   {detail}"
        )
        if r["skipped"]:
            print(f"{'':<26}（{r['skipped']} 题缺 guidelines，未审）")
        if r["errors_by_tag"]:
            top = sorted(r["errors_by_tag"].items(), key=lambda kv: -kv[1])[:5]
            print(f"{'':<26}高频标签：{'  '.join(f'{k} {v}' for k, v in top)}")

    print("\n口径提醒：critical_helped 是「把考卷改简单」，对模拟用户而言是最坏的一类错，")
    print("          它会让 Agent 分数虚高。绝不能因为 Agent 分数高就说模拟用户更好。")
    totals = usage.describe()
    price = price_for(args.reviewer)
    if price is None:
        print(f"\n[注意] 审查模型 {args.reviewer} 不在价格快照里，本次审查成本无法折算。")
    else:
        cost = price.cost(
            totals["prompt_tokens"], totals["completion_tokens"], totals["cached_tokens"]
        )
        print(
            f"\n审查用量：{totals['judge_calls']} 次调用，"
            f"输入 {totals['prompt_tokens']} tok（缓存 {totals['cached_tokens']}），"
            f"输出 {totals['completion_tokens']} tok，约 {cost:.4f} 元"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
