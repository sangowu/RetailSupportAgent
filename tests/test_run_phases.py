"""Tests for the early-stop phase logic.

Choosing a model in one round means there is no fallback tier, but there is
also no reason to run every task for a configuration that is clearly failing.
The phase split itself must be pure logic and testable: running it for real
costs money, so correctness cannot be established by running it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

_spec = importlib.util.spec_from_file_location("run_eval", REPO / "scripts" / "run_eval.py")
assert _spec and _spec.loader
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

TASKS = [str(i) for i in range(12)]


def sim(reward: float | None) -> SimpleNamespace:
    info = None if reward is None else SimpleNamespace(reward=reward)
    return SimpleNamespace(reward_info=info)


class TestPlanPhases:
    def test_no_early_stop_runs_everything_in_one_go(self) -> None:
        assert run_eval.plan_phases(TASKS, None) == [TASKS]

    def test_early_stop_splits_into_probe_and_remainder(self) -> None:
        phases = run_eval.plan_phases(TASKS, 4)
        assert phases == [TASKS[:4], TASKS[4:]]
        assert [t for phase in phases for t in phase] == TASKS, (
            "splitting must not drop tasks or reorder them"
        )

    @pytest.mark.parametrize("bad", [0, -1, 12, 99])
    def test_degenerate_values_fall_back_to_a_single_phase(self, bad: int) -> None:
        """An unreachable or invalid N must not produce an empty phase."""
        assert run_eval.plan_phases(TASKS, bad) == [TASKS]


class TestAverageReward:
    def test_none_when_nothing_is_scored(self) -> None:
        assert run_eval.average_reward([]) is None
        assert run_eval.average_reward([sim(None), sim(None)]) is None

    def test_unscored_simulations_are_excluded_not_counted_as_zero(self) -> None:
        """Counting an unscored task as 0 would fail a model that simply did not
        finish."""
        assert run_eval.average_reward([sim(1.0), sim(None)]) == pytest.approx(1.0)

    def test_all_zero_triggers_the_default_threshold(self) -> None:
        assert run_eval.average_reward([sim(0.0)] * 4) == pytest.approx(0.0)

    def test_one_success_clears_the_default_threshold(self) -> None:
        avg = run_eval.average_reward([sim(0.0), sim(0.0), sim(0.0), sim(1.0)])
        assert avg is not None and avg > 0.001


class TestSummarizeTakesMergedSimulations:
    def test_counts_across_phases(self) -> None:
        merged = [sim(1.0), sim(0.0), sim(1.0)]
        summary = run_eval.summarize(merged, expected=len(merged))
        assert summary["simulation_count"] == 3
        assert summary["scored_count"] == 3
        assert summary["pass_count"] == 2
        assert summary["avg_reward"] == pytest.approx(2 / 3)

    def test_empty_run_does_not_crash(self) -> None:
        summary = run_eval.summarize([], expected=0)
        assert summary["simulation_count"] == 0
        assert summary["avg_reward"] is None
