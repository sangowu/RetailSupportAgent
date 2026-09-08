"""Manifest self-consistency: no upstream checkout needed, runs offline in CI."""

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
    assert HEX40.match(upstream["commit"]), "must pin a full commit, not a tag or a branch"
    assert upstream["tag"] == "v1.0.1"
    assert upstream["license"] == "MIT"
    assert upstream["copyright"]


def test_every_pinned_file_has_a_valid_digest(manifest: dict[str, Any]) -> None:
    files = manifest["files_sha256"]
    assert files, "at minimum the policy, tasks, splits, tools and evaluator must be pinned"
    for path, digest in files.items():
        assert HEX64.match(digest), f"{path} does not have a valid SHA256 digest"
    for required in (
        "data/tau2/domains/retail/policy.md",
        "data/tau2/domains/retail/tasks.json",
        "data/tau2/domains/retail/split_tasks.json",
        "src/tau2/domains/retail/tools.py",
        "src/tau2/evaluator/evaluator.py",
        "LICENSE",
    ):
        assert required in files, f"a file that must be pinned is missing: {required}"


def test_task_counts_are_internally_consistent(manifest: dict[str, Any]) -> None:
    tasks = manifest["tasks"]
    splits = tasks["splits"]
    assert tasks["total"] == 114
    assert splits["base"] == tasks["total"]
    assert splits["train"] + splits["test"] == tasks["total"], (
        "train and test must cover the full set exactly"
    )
    assert sum(tasks["scope_label_counts"].values()) == tasks["total"]


def test_tool_inventory_matches_declared_count(manifest: dict[str, Any]) -> None:
    domain = manifest["domain"]
    tools = domain["tools"]
    assert domain["tool_count"] == len(tools) == 16
    names = {t["name"] for t in tools}
    # The tools the three declared flows depend on must exist, or the business
    # loop cannot be implemented.
    assert {"get_order_details", "cancel_pending_order", "return_delivered_order_items"} <= names
    assert {t["type"] for t in tools} <= {"READ", "WRITE", "GENERIC"}
