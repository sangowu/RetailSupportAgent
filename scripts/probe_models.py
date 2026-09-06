"""M0.2：对候选模型逐个执行能力探针，产出可复现的原始记录。

**本脚本会发起真实 API 调用并产生费用**（提示词极小、输出上限很低，成本可忽略）。

用法：
    uv run python scripts/probe_models.py                    # 全部候选模型
    uv run python scripts/probe_models.py --models glm-5.2   # 指定模型
    uv run python scripts/probe_models.py --dry-run          # 只列出将要执行的探针

结果写入 evaluation/capability_checks/<UTC 时间戳>/，含逐模型原始记录与汇总。
密钥不出现在任何输出中。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openai import OpenAI

from retail_support_agent import model_probe as mp
from retail_support_agent.config import CANDIDATE_MODELS, Settings
from retail_support_agent.tau2_pin import REPO_ROOT

OUTPUT_ROOT = REPO_ROOT / "evaluation" / "capability_checks"

# 逐档探测的力度等级取各家文档的并集：哪些被接受本身就是要测的结论，
# 而不是先假定某模型只支持某几档。
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


def probe_plan() -> list[tuple[str, Callable[[OpenAI, str], mp.ProbeOutcome]]]:
    """探针执行顺序。basic 必须在最前——它失败就没必要继续烧钱。"""
    plan: list[tuple[str, Callable[[OpenAI, str], mp.ProbeOutcome]]] = [
        ("basic", mp.probe_basic),
        ("tool_call", mp.probe_tool_call),
        ("tool_result_roundtrip", mp.probe_tool_result_roundtrip),
        ("stream_usage", mp.probe_stream_usage),
        ("sampling_params", mp.probe_sampling_params),
        ("truncation", mp.probe_truncation),
        ("thinking_off", mp.probe_thinking_off),
    ]
    # 先用小上限探出各档位映射的预算（报错即证据），再用不设上限的方式测实际生效情况
    plan += [
        (
            f"effort_budget_mapping_{level}",
            (lambda lv: lambda c, m: mp.probe_effort_budget_mapping(c, m, lv))(level),
        )
        for level in EFFORT_LEVELS
    ]
    plan += [
        (
            f"reasoning_effort_{level}",
            (lambda lv: lambda c, m: mp.probe_reasoning_effort(c, m, lv))(level),
        )
        for level in EFFORT_LEVELS
    ]
    plan += [
        ("thinking_budget_64", lambda c, m: mp.probe_thinking_budget(c, m, 64)),
        ("thinking_mutual_exclusion", mp.probe_mutual_exclusion),
    ]
    return plan


@dataclass
class ModelReport:
    """一个模型的全部探针结果。"""

    model_id_requested: str
    outcomes: list[mp.ProbeOutcome]

    def to_json(self) -> dict[str, Any]:
        return {
            "model_id_requested": self.model_id_requested,
            "probe_count": len(self.outcomes),
            "outcomes": [o.to_json() for o in self.outcomes],
        }


def run_model(client: OpenAI, model: str) -> ModelReport:
    """跑完一个模型的全部探针；模型不可用时如实记录并停止，不改用别的模型。"""
    plan = probe_plan()
    outcomes: list[mp.ProbeOutcome] = []

    print(f"\n=== {model} ===", flush=True)
    for name, probe in plan:
        outcome = probe(client, model)
        outcomes.append(outcome)
        detail = outcome.findings or (outcome.error or {})
        print(f"  {name:<28} {outcome.status:<9} {json.dumps(detail, ensure_ascii=False)[:110]}")

        if name == "basic" and outcome.status != "ok":
            print("  basic 失败，跳过该模型其余探针（不擅自替换模型）")
            break

    return ModelReport(model, outcomes)


def main() -> int:
    parser = argparse.ArgumentParser(description="M0.2 模型能力探针（会产生真实费用）")
    parser.add_argument("--models", nargs="*", default=list(CANDIDATE_MODELS))
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if args.dry_run:
        names = [name for name, _ in probe_plan()]
        total = len(args.models) * len(names)
        print(f"模型 {len(args.models)} 个 × 探针 {len(names)} 个 = {total} 次请求")
        print("模型：" + ", ".join(args.models))
        print("探针：" + ", ".join(names))
        return 0

    try:
        settings = Settings()  # type: ignore[call-arg]  # 字段由环境/.env 提供
    except Exception as exc:
        print(
            f"配置读取失败（检查 .env 中的 DASHSCOPE_API_KEY）：{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    client = mp.build_client(settings)
    run_dir = OUTPUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    reports = [run_model(client, model) for model in args.models]

    for report in reports:
        name = report.model_id_requested.replace("/", "_")
        (run_dir / f"{name}.json").write_text(
            json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    summary = {
        "run_at": datetime.now(UTC).isoformat(),
        "base_url": settings.base_url,
        "api_key": settings.key_presence(),  # 只记录"配了没配"，不含任何密钥内容
        "models": args.models,
        "note": "HTTP 200 不等于参数生效；结论须以 reasoning_tokens 差异或服务端报错为证据。",
        "per_model_status": {
            r.model_id_requested: {o.name: o.status for o in r.outcomes} for r in reports
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n原始记录已写入 {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
