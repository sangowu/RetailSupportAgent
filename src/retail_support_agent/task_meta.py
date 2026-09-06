"""读取上游 Retail 任务的评分构成，回答"这题到底考什么"。

存在的理由是一个实测发现：114 题里有 112 题在 `reward_basis` 里声明了
NL_ASSERTION，但其中 **72 题的断言列表是空的**——上游对空列表直接返回 1.0，
判官根本不会被调用。真正会走判官的只有 **40 题**。

后果有两个，都必须靠代码而不是靠记忆来防：
1. 冒烟若抽到无断言的题，会在"判官链路已验证"的假象下通过；
2. 判官选型的影响面被高估三倍，成本与风险评估都会失真。
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
    """一道题的评分构成。"""

    task_id: str
    reward_basis: tuple[str, ...]
    nl_assertion_count: int

    @property
    def invokes_judge(self) -> bool:
        """声明了 NL_ASSERTION 不算数，断言列表非空才会真的调判官。"""
        return self.nl_assertion_count > 0


@lru_cache(maxsize=1)
def load_task_facts(tasks_path: Path = TASKS_PATH) -> dict[str, TaskFacts]:
    if not tasks_path.is_file():
        raise FileNotFoundError(f"找不到上游任务文件 {tasks_path}，先运行 scripts/fetch_tau2.py")
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
    """给定划分里真正会调用判官的题号，保持冻结清单里的原始顺序。"""
    facts = load_task_facts()
    ids: list[str] = load_task_set(split)["task_ids"]
    return [tid for tid in ids if tid in facts and facts[tid].invokes_judge]


def split_composition(split: str) -> dict[str, int]:
    """一个划分的评分构成统计，用于报告里说明样本覆盖了什么。"""
    facts = load_task_facts()
    ids: list[str] = load_task_set(split)["task_ids"]
    known = [facts[tid] for tid in ids if tid in facts]
    return {
        "total": len(ids),
        "declares_nl_assertion": sum(1 for f in known if "NL_ASSERTION" in f.reward_basis),
        "invokes_judge": sum(1 for f in known if f.invokes_judge),
        "db_only": sum(1 for f in known if "NL_ASSERTION" not in f.reward_basis),
    }
