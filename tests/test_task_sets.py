"""Split discipline for the frozen task sets: dev and holdout must not overlap
or lose tasks."""

from __future__ import annotations

from typing import Any

import pytest

from retail_support_agent.tau2_pin import load_manifest, load_task_set


@pytest.fixture(scope="module")
def sets() -> dict[str, Any]:
    return {name: load_task_set(name) for name in ("dev", "holdout", "regression", "scope_labels")}


def test_dev_and_holdout_are_disjoint(sets: dict[str, Any]) -> None:
    dev, holdout = set(sets["dev"]["task_ids"]), set(sets["holdout"]["task_ids"])
    assert dev & holdout == set(), (
        "an overlap makes the holdout no longer an independent verification"
    )


def test_sizes_match_manifest(sets: dict[str, Any]) -> None:
    splits = load_manifest()["tasks"]["splits"]
    assert sets["dev"]["count"] == len(sets["dev"]["task_ids"]) == splits["train"]
    assert sets["holdout"]["count"] == len(sets["holdout"]["task_ids"]) == splits["test"]


def test_scope_labels_cover_every_task(sets: dict[str, Any]) -> None:
    labels = sets["scope_labels"]["labels"]
    all_ids = set(sets["dev"]["task_ids"]) | set(sets["holdout"]["task_ids"])
    assert set(labels) == all_ids, (
        "scope labels must cover every task or the layered report loses tasks"
    )
    assert set(labels.values()) <= set(sets["scope_labels"]["definitions"])


def test_regression_set_is_versioned_and_starts_empty(sets: dict[str, Any]) -> None:
    regression = sets["regression"]
    assert regression["version"], (
        "the regression set needs a version so reports can cite a snapshot"
    )
    assert regression["count"] == len(regression["task_ids"])
    # No evaluation had run at M0.1, so an empty regression set is a fact,
    # not a defect.
    assert regression["count"] == 0
