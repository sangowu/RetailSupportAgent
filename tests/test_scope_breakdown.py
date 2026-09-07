"""按业务范围分层报告。

M0.1 定的规矩：**总分与 42 题子集分都要给，两者不互相冒充**。
只报总分会掩盖"本项目声明支持的业务上表现如何"；只报子集分则不能再声称
跑的是标准 τ²-bench Retail。

这里守两件事：42 题这个子集的定义没有漂移；样本不够时拒绝报分而不是硬给一个数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from analyze_eval import IN_SCOPE_LABELS, print_scope_breakdown  # noqa: E402


def _sim(task_id: str, reward: float | None) -> dict[str, Any]:
    return {"task_id": task_id, "reward_info": ({"reward": reward} if reward is not None else {})}


class TestTheSubsetDefinition:
    def test_in_scope_is_exactly_read_only_plus_in_scope_write(self) -> None:
        """42 = 只读 8 + 范围内写 34。这个定义漂了，"42 题子集分"就换了含义。"""
        assert set(IN_SCOPE_LABELS) == {"read_only", "in_scope_write"}

    def test_the_subset_really_has_42_tasks(self) -> None:
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        in_scope = [t for t, label in labels.items() if label in IN_SCOPE_LABELS]
        assert len(in_scope) == 42, "子集大小变了——M0.1 声明的是 114 题里的 42 题"
        assert len(labels) == 114


class TestReporting:
    def test_small_sample_is_refused_not_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        """样本不够就说清楚，不要硬报一个数。

        holdout 只有 13 题落在这个子集里——13 道题撑不住任何结论，
        而一个印出来的比例看上去和全集分一样权威。
        """
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        in_scope = [t for t, label in labels.items() if label in IN_SCOPE_LABELS][:5]
        print_scope_breakdown([_sim(t, 1.0) for t in in_scope])
        out = capsys.readouterr().out
        assert "样本太少，不单独报分" in out
        assert "= 1.0000" not in out.split("样本太少")[-1]

    def test_full_sample_reports_both_scopes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """够样本时两个口径都要出现，并明说它们不互相冒充。"""
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        sims = [_sim(t, 1.0 if label in IN_SCOPE_LABELS else 0.0) for t, label in labels.items()]
        print_scope_breakdown(sims)
        out = capsys.readouterr().out
        assert "42 题子集）：42/42 = 1.0000" in out
        assert "两者不互相冒充" in out
        # 范围外那 70 题全判 0，分层里必须如实反映，而不是被总分平均掉
        assert "0/70" in out

    def test_unscored_tasks_are_left_out_of_every_bucket(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """没拿到分数的题不能进分母，否则丢题会让分层成绩虚低。"""
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        in_scope = [t for t, label in labels.items() if label in IN_SCOPE_LABELS]
        sims = [_sim(t, 1.0) for t in in_scope[:30]] + [_sim(t, None) for t in in_scope[30:]]
        print_scope_breakdown(sims)
        out = capsys.readouterr().out
        assert "30 题" in out, "未评分的题混进了分母"
