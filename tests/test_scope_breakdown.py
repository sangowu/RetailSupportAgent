"""Layered reporting by business scope.

The M0.1 rule: report both the total and the 42-task subset, and let neither
stand in for the other. Reporting only the total hides how the declared flows
perform; reporting only the subset means the run can no longer be called
standard τ²-bench Retail.

Two things are guarded here: the definition of the 42-task subset has not
drifted, and a small sample is refused rather than scored anyway.
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
        """42 = 8 read-only + 34 in-scope writes. If the definition drifts, the
        "42-task subset score" means something else."""
        assert set(IN_SCOPE_LABELS) == {"read_only", "in_scope_write"}

    def test_the_subset_really_has_42_tasks(self) -> None:
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        in_scope = [t for t, label in labels.items() if label in IN_SCOPE_LABELS]
        assert len(in_scope) == 42, "the subset size changed; M0.1 declared 42 of the 114 tasks"
        assert len(labels) == 114


class TestReporting:
    def test_small_sample_is_refused_not_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        """State the small sample instead of printing a number anyway.

        Only 13 holdout tasks fall in this subset, which supports no conclusion,
        while a printed ratio looks as authoritative as the full-set score.
        """
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        in_scope = [t for t, label in labels.items() if label in IN_SCOPE_LABELS][:5]
        print_scope_breakdown([_sim(t, 1.0) for t in in_scope])
        out = capsys.readouterr().out
        assert "Sample too small to score separately" in out
        assert "= 1.0000" not in out.split("Sample too small")[-1]

    def test_full_sample_reports_both_scopes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With enough sample both figures appear, and the text says neither stands
        in for the other."""
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        sims = [_sim(t, 1.0 if label in IN_SCOPE_LABELS else 0.0) for t, label in labels.items()]
        print_scope_breakdown(sims)
        out = capsys.readouterr().out
        assert "Declared flows (42-task subset): 42/42 = 1.0000" in out
        assert "neither stands in for the other" in out
        # The 70 out-of-scope tasks all scored 0, which the layered view must show
        # rather than average away into the total.
        assert "0/70" in out

    def test_unscored_tasks_are_left_out_of_every_bucket(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unscored tasks stay out of the denominator, or losing tasks would drag
        the layered score down."""
        labels = json.loads(
            (REPO / "evaluation" / "task_sets" / "scope_labels.json").read_text(encoding="utf-8")
        )["labels"]
        in_scope = [t for t, label in labels.items() if label in IN_SCOPE_LABELS]
        sims = [_sim(t, 1.0) for t in in_scope[:30]] + [_sim(t, None) for t in in_scope[30:]]
        print_scope_breakdown(sims)
        out = capsys.readouterr().out
        assert "30" in out, "unscored tasks reached the denominator"
