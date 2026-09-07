"""把探针原始记录汇总成能力矩阵（Markdown + JSON）。

矩阵必须由原始记录生成，不能手打——手打的表格没法追溯到具体请求，
也就没法在结论被质疑时给出证据。

用法：
    uv run python scripts/summarize_capability.py                # 最新一次运行
    uv run python scripts/summarize_capability.py --run 20260906T123218Z
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_support_agent.tau2_pin import REPO_ROOT

RUNS_ROOT = REPO_ROOT / "evaluation" / "capability_checks"

# 基础能力探针 -> 判定该探针"通过"所依据的 findings 字段
BASELINE_CHECKS: dict[str, str] = {
    "basic": "callable",
    "tool_call": "order_id_is_string",
    "tool_result_roundtrip": "answered_after_tool_result",
    "stream_usage": "usage_in_stream",
    "sampling_params": "accepted",
    "truncation": "signals_length_limit",
    "thinking_off": "thinking_actually_off",
}

EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


def load_run(run_dir: Path) -> dict[str, dict[str, Any]]:
    """读入一次运行的全部模型记录，按探针名索引。"""
    models: dict[str, dict[str, Any]] = {}
    for path in sorted(run_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        models[report["model_id_requested"]] = {
            outcome["name"]: outcome for outcome in report["outcomes"]
        }
    return models


def mark(outcome: dict[str, Any] | None, key: str) -> str:
    if outcome is None:
        return "—"
    if outcome["status"] != "ok":
        return "✗"
    return "✓" if outcome.get("findings", {}).get(key) else "✗"


def table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def build_markdown(run_id: str, models: dict[str, dict[str, Any]]) -> str:
    names = list(models)
    parts: list[str] = [
        f"# 模型能力矩阵（运行 {run_id}）",
        "",
        "本文件由 `scripts/summarize_capability.py` 从原始探针记录生成，请勿手工编辑。",
        "",
        "## 基础能力",
        "",
        table(
            ["探针", *names],
            [
                [probe, *[mark(models[m].get(probe), key) for m in names]]
                for probe, key in BASELINE_CHECKS.items()
            ],
        ),
        "",
        "## reasoning_effort 各档位：是否被接受 / 实测 reasoning_tokens",
        "",
    ]

    rows: list[list[str]] = []
    for level in EFFORT_LEVELS:
        row = [level]
        for model in names:
            outcome = models[model].get(f"reasoning_effort_{level}")
            if outcome is None:
                row.append("—")
            elif outcome["status"] != "ok":
                row.append("拒绝")
            else:
                tokens = outcome["findings"].get("reasoning_tokens")
                row.append("接受 / 无思考" if tokens is None else f"接受 / {tokens}")
        rows.append(row)
    parts += [table(["档位", *names], rows), ""]

    parts += ["## 档位映射的思考预算（由服务端报错反推）", ""]
    rows = []
    for level in EFFORT_LEVELS:
        row = [level]
        for model in names:
            outcome = models[model].get(f"effort_budget_mapping_{level}")
            findings = (outcome or {}).get("findings", {})
            mapped = findings.get("mapped_thinking_budget")
            if outcome is None:
                row.append("—")
            elif findings.get("level_unsupported"):
                row.append("不支持")
            elif mapped is not None:
                row.append(str(mapped))
            else:
                row.append("未暴露")
        rows.append(row)
    parts += [
        table(["档位", *names], rows),
        "",
        "「未暴露」表示该档位在 512 输出上限下就能跑通，服务端没有报出预算数字，不代表预算为零。",
        "",
        "## 思考预算与互斥规则",
        "",
    ]

    rows = []
    for model in names:
        budget = models[model].get("thinking_budget_64", {})
        bf = budget.get("findings", {})
        mutual = models[model].get("thinking_mutual_exclusion", {})
        mf = mutual.get("findings", {})
        within = bf.get("within_budget")
        rows.append(
            [
                model,
                str(bf.get("reasoning_tokens", "—")),
                {True: "遵守", False: "**超出**", None: "—"}[within],
                "拒绝" if mf.get("server_rejects_both") else "静默接受",
                str(mf.get("winner", "—")),
            ]
        )
    parts += [
        table(
            ["模型", "budget=64 实测思考 token", "是否遵守上限", "同时传两参数", "实际生效的参数"],
            rows,
        ),
        "",
    ]
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总模型能力探针结果")
    parser.add_argument("--run", default=None, help="运行目录名，默认取最新一次")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    runs = sorted(p for p in RUNS_ROOT.iterdir() if p.is_dir()) if RUNS_ROOT.is_dir() else []
    if not runs:
        print("没有找到探针记录，先运行 scripts/probe_models.py", file=sys.stderr)
        return 2
    run_dir = (RUNS_ROOT / args.run) if args.run else runs[-1]
    if not run_dir.is_dir():
        print(f"运行目录不存在：{run_dir}", file=sys.stderr)
        return 2

    models = load_run(run_dir)
    markdown = build_markdown(run_dir.name, models)
    (run_dir / "matrix.md").write_text(markdown, encoding="utf-8", newline="\n")
    print(markdown)
    print(f"已写入 {run_dir / 'matrix.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
