"""从本地固定检出重新生成 evaluation/tau2_manifest.json 与任务集清单。

manifest 必须可复现生成，而不是手工维护的一堆数字。上游 commit 变更或需要
重新核对事实时运行本脚本，再用 git diff 审查产物变化。

摘要针对上游**提交时的原始字节**计算：`scripts/fetch_tau2.py` 会以
`core.autocrlf=false` 检出，确保 Windows 与 Linux 得到同一组 SHA256。

用法：
    uv run python scripts/fetch_tau2.py --verify-only   # 先确认检出干净
    uv run python scripts/regen_manifest.py
"""

from __future__ import annotations

import collections
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_support_agent.tau2_pin import (
    DEFAULT_CHECKOUT,
    MANIFEST_PATH,
    REPO_ROOT,
    TASK_SETS_DIR,
    sha256_of,
)

# 固定的文件清单：政策、数据、划分、模拟用户指引、retail 实现与评分器。
PINNED_FILES = [
    "LICENSE",
    "data/tau2/domains/retail/db.json",
    "data/tau2/domains/retail/policy.md",
    "data/tau2/domains/retail/tasks.json",
    "data/tau2/domains/retail/split_tasks.json",
    "data/tau2/user_simulator/simulation_guidelines.md",
    "src/tau2/domains/retail/tools.py",
    "src/tau2/domains/retail/data_model.py",
    "src/tau2/domains/retail/environment.py",
    "src/tau2/domains/retail/utils.py",
    "src/tau2/evaluator/evaluator.py",
    "src/tau2/evaluator/evaluator_env.py",
    "src/tau2/evaluator/evaluator_nl_assertions.py",
]

# 本项目声明的三类业务（查询 / 取消 / 退货）所对应的写操作工具
IN_SCOPE_WRITE = frozenset({"cancel_pending_order", "return_delivered_order_items"})
ALL_WRITE = frozenset(
    {
        "cancel_pending_order",
        "return_delivered_order_items",
        "exchange_delivered_order_items",
        "modify_pending_order_items",
        "modify_pending_order_address",
        "modify_pending_order_payment",
        "modify_user_address",
    }
)


def git_out(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=checkout, capture_output=True, text=True, check=True
    ).stdout.strip()


def scope_label(actions: list[dict[str, Any]]) -> str:
    """按评分动作中的写操作判断任务落在哪个业务范围。"""
    if not actions:
        return "no_action"
    writes = {a["name"] for a in actions} & ALL_WRITE
    if not writes:
        return "read_only"
    return "in_scope_write" if writes <= IN_SCOPE_WRITE else "out_of_scope_write"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(checkout: Path) -> None:
    commit = git_out(checkout, "rev-parse", "HEAD")
    commit_date = git_out(checkout, "log", "-1", "--format=%ad", "--date=iso-strict")

    tools_src = (checkout / "src/tau2/domains/retail/tools.py").read_text(encoding="utf-8")
    tools = [
        {"name": name, "type": kind.split(".")[-1]}
        for kind, name in re.findall(r"@is_tool\((\w+\.\w+)\)\s*\n\s*def (\w+)", tools_src)
    ]

    retail = checkout / "data/tau2/domains/retail"
    tasks: list[dict[str, Any]] = json.loads((retail / "tasks.json").read_text(encoding="utf-8"))
    split: dict[str, list[str]] = json.loads(
        (retail / "split_tasks.json").read_text(encoding="utf-8")
    )

    labels: dict[str, str] = {}
    reward_basis: collections.Counter[str] = collections.Counter()
    for task in tasks:
        criteria = task.get("evaluation_criteria") or {}
        reward_basis[",".join(criteria.get("reward_basis") or [])] += 1
        labels[task["id"]] = scope_label(criteria.get("actions") or [])

    train, test = split["train"], split["test"]
    if set(train) & set(test):
        raise SystemExit("上游 train/test 划分出现重叠，协议不可用")
    if len(split["base"]) != len(tasks):
        raise SystemExit("base 划分与任务总数不一致，协议不可用")

    manifest = {
        "$comment": (
            "τ²-bench 上游固定记录，由 scripts/regen_manifest.py 生成，不要手工编辑。"
            "摘要针对上游提交时的原始字节（LF），跨平台一致。"
        ),
        "generated_at": "2026-09-06",
        "upstream": {
            "name": "Sierra tau2-bench",
            "repo": "https://github.com/sierra-research/tau2-bench",
            "tag": "v1.0.1",
            "commit": commit,
            "commit_date": commit_date,
            "license": "MIT",
            "copyright": "Copyright (c) 2025 Sierra Research",
            "attribution": (
                "数据、工具、政策与评分器均为上游原创，本项目仅调用，不得宣称为自研实现。"
            ),
        },
        "domain": {
            "name": "retail",
            "policy_file": "data/tau2/domains/retail/policy.md",
            "db_file": "data/tau2/domains/retail/db.json",
            "tasks_file": "data/tau2/domains/retail/tasks.json",
            "split_file": "data/tau2/domains/retail/split_tasks.json",
            "solo_mode_supported": False,
            "tool_count": len(tools),
            "tools": tools,
        },
        "tasks": {
            "total": len(tasks),
            "splits": {"base": len(split["base"]), "train": len(train), "test": len(test)},
            "reward_basis_distribution": dict(reward_basis),
            "scope_label_counts": dict(collections.Counter(labels.values())),
        },
        "scoring": {
            "reward_rule": "各生效分量奖励相乘；分量由任务 reward_basis 决定",
            "retail_basis": "112 题为 DB + NL_ASSERTION，2 题为纯 DB",
            "db_component": "对比最终数据库状态（src/tau2/evaluator/evaluator_env.py）",
            "nl_assertion_component": (
                "LLM 评判（src/tau2/evaluator/evaluator_nl_assertions.py），"
                "需固定评判模型且不随被测模型切换"
            ),
            "note": "communicate 与 action 分量不在 retail 的 reward_basis 中，仅作诊断用途。",
        },
        "files_sha256": {rel: sha256_of(checkout / rel) for rel in PINNED_FILES},
    }
    write_json(MANIFEST_PATH, manifest)

    write_json(
        TASK_SETS_DIR / "dev.json",
        {
            "$comment": "开发集＝上游 train 划分。仅此集合可用于筛选、调参与提示词迭代。",
            "source": "tau2-bench v1.0.1 split_tasks.json:train",
            "count": len(train),
            "task_ids": train,
        },
    )
    write_json(
        TASK_SETS_DIR / "holdout.json",
        {
            "$comment": "保留验证集＝上游 test 划分。仅用于入围配置的独立验证，不得用于调参。",
            "source": "tau2-bench v1.0.1 split_tasks.json:test",
            "count": len(test),
            "task_ids": test,
        },
    )
    write_json(
        TASK_SETS_DIR / "scope_labels.json",
        {
            "$comment": "业务范围标签，用于分层报告；不改变上游任务与评分，只影响结果切片。",
            "definitions": {
                "read_only": "评分动作不含任何写操作工具",
                "in_scope_write": (
                    f"写操作仅涉及 {sorted(IN_SCOPE_WRITE)}（本项目声明的取消/退货范围）"
                ),
                "out_of_scope_write": "涉及改单/换货/改地址/改支付等超出三类业务声明的写操作",
                "no_action": "上游未给出评分动作",
            },
            "counts": dict(collections.Counter(labels.values())),
            "labels": labels,
        },
    )
    # regression.json 记录跑测后暴露的失败题，由人工/评测流程维护，此处不覆盖。

    print(f"已按 {commit[:12]} 重新生成 manifest 与任务集（regression.json 未改动）")
    print(f"输出目录：{REPO_ROOT / 'evaluation'}")


if __name__ == "__main__":
    if not (DEFAULT_CHECKOUT / ".git").is_dir():
        raise SystemExit(f"上游检出不存在：{DEFAULT_CHECKOUT}（先运行 scripts/fetch_tau2.py）")
    build(DEFAULT_CHECKOUT)
