"""manifest 自洽性检查：不依赖上游检出，可在 CI 离线运行。"""

from __future__ import annotations

import re
from typing import Any

import pytest

from retail_support_agent.tau2_pin import load_manifest

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return load_manifest()


def test_upstream_is_pinned_to_exact_commit(manifest: dict[str, Any]) -> None:
    upstream = manifest["upstream"]
    assert HEX40.match(upstream["commit"]), "必须固定到完整 commit，不能只写 tag 或分支"
    assert upstream["tag"] == "v1.0.1"
    assert upstream["license"] == "MIT"
    assert upstream["copyright"]


def test_every_pinned_file_has_a_valid_digest(manifest: dict[str, Any]) -> None:
    files = manifest["files_sha256"]
    assert files, "至少要固定政策、任务、划分、工具与评分器文件"
    for path, digest in files.items():
        assert HEX64.match(digest), f"{path} 的摘要不是合法 SHA256"
    for required in (
        "data/tau2/domains/retail/policy.md",
        "data/tau2/domains/retail/tasks.json",
        "data/tau2/domains/retail/split_tasks.json",
        "src/tau2/domains/retail/tools.py",
        "src/tau2/evaluator/evaluator.py",
        "LICENSE",
    ):
        assert required in files, f"缺少必须固定的文件：{required}"


def test_task_counts_are_internally_consistent(manifest: dict[str, Any]) -> None:
    tasks = manifest["tasks"]
    splits = tasks["splits"]
    assert tasks["total"] == 114
    assert splits["base"] == tasks["total"]
    assert splits["train"] + splits["test"] == tasks["total"], "train/test 必须正好覆盖全集"
    assert sum(tasks["scope_label_counts"].values()) == tasks["total"]


def test_tool_inventory_matches_declared_count(manifest: dict[str, Any]) -> None:
    domain = manifest["domain"]
    tools = domain["tools"]
    assert domain["tool_count"] == len(tools) == 16
    names = {t["name"] for t in tools}
    # 本项目声明的三类业务所依赖的工具必须存在，否则业务闭环无法实现
    assert {"get_order_details", "cancel_pending_order", "return_delivered_order_items"} <= names
    assert {t["type"] for t in tools} <= {"READ", "WRITE", "GENERIC"}
