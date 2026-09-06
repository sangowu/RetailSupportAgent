"""提前终止分段逻辑的测试。

一轮定模型意味着没有后续档位兜底，但也没必要给明显不合格的配置把题跑完。
分段本身必须是纯逻辑、可测的——真跑起来要花钱，不能靠跑一遍来验证它对不对。
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
        assert [t for phase in phases for t in phase] == TASKS, "分段不得丢题或改顺序"

    @pytest.mark.parametrize("bad", [0, -1, 12, 99])
    def test_degenerate_values_fall_back_to_a_single_phase(self, bad: int) -> None:
        """N 够不着或不合法时不该切出空段。"""
        assert run_eval.plan_phases(TASKS, bad) == [TASKS]


class TestAverageReward:
    def test_none_when_nothing_is_scored(self) -> None:
        assert run_eval.average_reward([]) is None
        assert run_eval.average_reward([sim(None), sim(None)]) is None

    def test_unscored_simulations_are_excluded_not_counted_as_zero(self) -> None:
        """把未评分当成 0 分会误杀一个其实没跑完的模型。"""
        assert run_eval.average_reward([sim(1.0), sim(None)]) == pytest.approx(1.0)

    def test_all_zero_triggers_the_default_threshold(self) -> None:
        assert run_eval.average_reward([sim(0.0)] * 4) == pytest.approx(0.0)

    def test_one_success_clears_the_default_threshold(self) -> None:
        avg = run_eval.average_reward([sim(0.0), sim(0.0), sim(0.0), sim(1.0)])
        assert avg is not None and avg > 0.001


class TestSummarizeTakesMergedSimulations:
    def test_counts_across_phases(self) -> None:
        merged = [sim(1.0), sim(0.0), sim(1.0)]
        summary = run_eval.summarize(merged)
        assert summary["simulation_count"] == 3
        assert summary["scored_count"] == 3
        assert summary["pass_count"] == 2
        assert summary["avg_reward"] == pytest.approx(2 / 3)

    def test_empty_run_does_not_crash(self) -> None:
        summary = run_eval.summarize([])
        assert summary["simulation_count"] == 0
        assert summary["avg_reward"] is None
