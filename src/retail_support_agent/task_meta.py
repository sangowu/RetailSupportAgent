"""Read the reward composition of upstream Retail tasks: what each task actually checks.

This module exists because of a measurement: 112 of 114 tasks declare NL_ASSERTION in
`reward_basis`, but 72 of those have an empty assertion list, and upstream returns 1.0
for an empty list without calling the judge. Only 40 tasks actually reach the judge.

Two consequences, both guarded by code rather than by memory:
1. a smoke run that happens to pick assertion-free tasks passes while appearing to
   have exercised the judge;
2. the impact of judge selection is overstated threefold, distorting cost and risk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from retail_support_agent.tau2_pin import DEFAULT_CHECKOUT, load_task_set

TASKS_PATH = DEFAULT_CHECKOUT / "data" / "tau2" / "domains" / "retail" / "tasks.json"


@dataclass(frozen=True)
class TaskFacts:
    """The reward composition of one task."""

    task_id: str
    reward_basis: tuple[str, ...]
    nl_assertion_count: int

    @property
    def invokes_judge(self) -> bool:
        """Declaring NL_ASSERTION is not enough; the judge runs only for a non-empty list."""
        return self.nl_assertion_count > 0


@lru_cache(maxsize=1)
def load_task_facts(tasks_path: Path = TASKS_PATH) -> dict[str, TaskFacts]:
    if not tasks_path.is_file():
        raise FileNotFoundError(
            f"upstream task file not found: {tasks_path}. Run scripts/fetch_tau2.py first"
        )
    raw: Any = json.loads(tasks_path.read_text(encoding="utf-8"))
    tasks = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    facts: dict[str, TaskFacts] = {}
    for task in tasks:
        criteria = task.get("evaluation_criteria") or {}
        task_id = str(task.get("id"))
        facts[task_id] = TaskFacts(
            task_id=task_id,
            reward_basis=tuple(criteria.get("reward_basis") or []),
            nl_assertion_count=len(criteria.get("nl_assertions") or []),
        )
    return facts


def judge_bearing_ids(split: str) -> list[str]:
    """Task ids in a split that actually invoke the judge, in frozen-list order."""
    facts = load_task_facts()
    ids: list[str] = load_task_set(split)["task_ids"]
    return [tid for tid in ids if tid in facts and facts[tid].invokes_judge]


def split_composition(split: str) -> dict[str, int]:
    """Reward composition of a split, used in reports to state what a sample covers."""
    facts = load_task_facts()
    ids: list[str] = load_task_set(split)["task_ids"]
    known = [facts[tid] for tid in ids if tid in facts]
    return {
        "total": len(ids),
        "declares_nl_assertion": sum(1 for f in known if "NL_ASSERTION" in f.reward_basis),
        "invokes_judge": sum(1 for f in known if f.invokes_judge),
        "db_only": sum(1 for f in known if "NL_ASSERTION" not in f.reward_basis),
    }
