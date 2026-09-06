"""冻结任务集的划分纪律：开发集与保留集不得重叠或漏题。"""

from __future__ import annotations

from typing import Any

import pytest

from retail_support_agent.tau2_pin import load_manifest, load_task_set


@pytest.fixture(scope="module")
def sets() -> dict[str, Any]:
    return {name: load_task_set(name) for name in ("dev", "holdout", "regression", "scope_labels")}


def test_dev_and_holdout_are_disjoint(sets: dict[str, Any]) -> None:
    dev, holdout = set(sets["dev"]["task_ids"]), set(sets["holdout"]["task_ids"])
    assert dev & holdout == set(), "保留集一旦与开发集重叠，独立验证结论即失效"


def test_sizes_match_manifest(sets: dict[str, Any]) -> None:
    splits = load_manifest()["tasks"]["splits"]
    assert sets["dev"]["count"] == len(sets["dev"]["task_ids"]) == splits["train"]
    assert sets["holdout"]["count"] == len(sets["holdout"]["task_ids"]) == splits["test"]


def test_scope_labels_cover_every_task(sets: dict[str, Any]) -> None:
    labels = sets["scope_labels"]["labels"]
    all_ids = set(sets["dev"]["task_ids"]) | set(sets["holdout"]["task_ids"])
    assert set(labels) == all_ids, "范围标签必须覆盖全部任务，否则分层报告会漏题"
    assert set(labels.values()) <= set(sets["scope_labels"]["definitions"])


def test_regression_set_is_versioned_and_starts_empty(sets: dict[str, Any]) -> None:
    regression = sets["regression"]
    assert regression["version"], "回归集必须带版本号，便于报告引用具体快照"
    assert regression["count"] == len(regression["task_ids"])
    # M0.1 尚未跑过评测，回归集为空是事实而非缺陷
    assert regression["count"] == 0
