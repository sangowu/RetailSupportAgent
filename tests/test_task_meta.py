"""Tests for the reward composition of the tasks.

These numbers were measured from the upstream task file rather than copied
from documentation. The claim that "112 tasks have NL assertions" does not
hold: 112 tasks declare NL_ASSERTION, and 72 of those have an empty assertion
list, for which upstream returns 1.0 without calling the judge. Only 40 tasks
reach the judge. Pinning the numbers means an upstream change to the task set
breaks the tests immediately.
"""

from __future__ import annotations

import pytest

from retail_support_agent.task_meta import (
    TASKS_PATH,
    TaskFacts,
    judge_bearing_ids,
    load_task_facts,
    split_composition,
)

pytestmark = pytest.mark.skipif(
    not TASKS_PATH.is_file(),
    reason="needs the third_party upstream checkout; see scripts/fetch_tau2.py",
)


def test_declared_nl_assertion_is_not_the_same_as_actually_judged() -> None:
    facts = load_task_facts()
    assert len(facts) == 114
    declared = [f for f in facts.values() if "NL_ASSERTION" in f.reward_basis]
    judged = [f for f in facts.values() if f.invokes_judge]
    assert len(declared) == 112, "the upstream task set changed; report definitions must follow"
    assert len(judged) == 40, (
        "the number of judge-invoking tasks changed; re-assess how much judge selection matters"
    )
    assert len(judged) < len(declared), (
        "the difference is the tasks whose empty assertion list scores a free 1.0"
    )


def test_empty_assertion_list_does_not_invoke_the_judge() -> None:
    fact = TaskFacts(task_id="x", reward_basis=("DB", "NL_ASSERTION"), nl_assertion_count=0)
    assert fact.invokes_judge is False


def test_dev_split_composition() -> None:
    composition = split_composition("dev")
    assert composition["total"] == 74
    assert composition["invokes_judge"] == 29


def test_holdout_split_composition() -> None:
    composition = split_composition("holdout")
    assert composition["total"] == 40
    assert composition["invokes_judge"] == 11


def test_judge_bearing_ids_preserve_frozen_order() -> None:
    """Sampling follows the frozen list order, or different machines sample
    different tasks."""
    ids = judge_bearing_ids("dev")
    assert ids[:3] == ["2", "3", "4"]
    assert len(ids) == 29


def test_the_original_pilot_tasks_never_exercised_the_judge() -> None:
    """Regression guard: the first two dev tasks have no assertions, so a smoke
    run using them would not exercise the judge at all."""
    facts = load_task_facts()
    assert facts["0"].invokes_judge is False
    assert facts["1"].invokes_judge is False
