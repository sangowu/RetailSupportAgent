"""任务评分构成的测试。

这些数字是从上游任务文件实测出来的，不是抄文档的——文档里"112 题有 NL 断言"
的说法与实际不符：112 题只是**声明**了 NL_ASSERTION，其中 72 题的断言列表是空的，
上游对空列表直接返回 1.0，判官不会被调用。真正会走判官的是 40 题。
把这几个数字钉成断言，是为了上游一旦改动任务集，测试立刻炸给我们看。
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
    not TASKS_PATH.is_file(), reason="需要 third_party 上游检出，见 scripts/fetch_tau2.py"
)


def test_declared_nl_assertion_is_not_the_same_as_actually_judged() -> None:
    facts = load_task_facts()
    assert len(facts) == 114
    declared = [f for f in facts.values() if "NL_ASSERTION" in f.reward_basis]
    judged = [f for f in facts.values() if f.invokes_judge]
    assert len(declared) == 112, "上游任务集变了，报告里的口径需要跟着改"
    assert len(judged) == 40, "真正调用判官的题数变了，判官选型的影响面需要重估"
    assert len(judged) < len(declared), "这正是空断言列表被当成满分的那批题"


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
    """抽样必须落在冻结清单的原始顺序上，否则不同机器抽出的样本不同。"""
    ids = judge_bearing_ids("dev")
    assert ids[:3] == ["2", "3", "4"]
    assert len(ids) == 29


def test_the_original_pilot_tasks_never_exercised_the_judge() -> None:
    """回归保护：开发集最前面两题没有断言，冒烟若用它们等于没测判官。"""
    facts = load_task_facts()
    assert facts["0"].invokes_judge is False
    assert facts["1"].invokes_judge is False
