"""A lost task must be noticed and must not produce a better-looking average.

This is a measured defect, not a hypothesis: on 2026-09-07 a 40-task holdout run had 5
tasks that never started because the provider quota ran out. Upstream dropped those 5
from the denominator and `avg_reward` read 0.886, while the completed run scored 0.875.
Losing tasks improved the score, which is far more dangerous than a crash.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from resume_run import rebuild_stack, unscored_task_ids
from retail_support_agent.config import Settings
from retail_support_agent.evaluation import EvalStack
from retail_support_agent.results_io import is_scored, load_simulations
from run_eval import summarize


class FakeSim:
    """summarize() reads attributes, not dict keys."""

    def __init__(self, task_id: str, reward: float | None, reason: str) -> None:
        self.task_id = task_id
        self.termination_reason = reason
        self.reward_info = type("RI", (), {"reward": reward})() if reward is not None else None
        self.agent_cost = None


@pytest.fixture
def run_dir() -> Iterator[Path]:
    """Build a temporary directory instead of using pytest's tmp_path.

    On this machine `Temp/pytest-of-*` is a leftover with broken permissions that cannot
    be removed, and tmp_path raises PermissionError. Unrelated to the code under test.
    """
    with tempfile.TemporaryDirectory(prefix="rsa_run_") as d:
        yield Path(d)


def write_run(tmp_path: Path, sims: list[dict[str, Any]], name: str = "part1.json") -> Path:
    part = tmp_path / name
    part.mkdir(parents=True, exist_ok=True)
    (part / "results.json").write_text(
        json.dumps({"simulations": sims}, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def sim(task_id: str, reward: float | None) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "trial": 0,
        "reward_info": {"reward": reward},
        "termination_reason": "user_stop" if reward is not None else "infrastructure_error",
        "messages": [],
    }


class TestSummarizeReconcilesAgainstExpected:
    def test_missing_tasks_are_flagged(self) -> None:
        sims = [FakeSim("0", 1.0, "user_stop"), FakeSim("1", None, "infrastructure_error")]
        out = summarize(sims, expected=2)
        assert out["incomplete"] is True
        assert out["unscored_task_ids"] == ["1"]
        assert out["scored_count"] == 1
        assert out["expected_count"] == 2

    def test_complete_run_is_not_flagged(self) -> None:
        sims = [FakeSim("0", 1.0, "user_stop"), FakeSim("1", 0.0, "user_stop")]
        assert summarize(sims, expected=2)["incomplete"] is False

    def test_avg_reward_is_still_the_partial_average(self) -> None:
        """avg_reward keeps its meaning; what changed is that an incomplete flag sits
        next to it.

        Quietly switching the denominator to `expected` would be worse: that scores
        tasks that never ran as 0, which is equally invented. The right response is to
        refuse to treat the number as the run's score.
        """
        sims = [FakeSim("0", 1.0, "user_stop"), FakeSim("1", None, "infrastructure_error")]
        out = summarize(sims, expected=2)
        assert out["avg_reward"] == 1.0
        assert out["incomplete"] is True


class TestResumedTasksAreDeduplicated:
    def test_scored_record_replaces_the_failed_one(self, run_dir: Path) -> None:
        """A resume makes one task appear twice; counting both inflates the simulation
        count and keeps the gap from ever reaching zero."""
        write_run(run_dir, [sim("0", 1.0), sim("1", None)], "part1.json")
        write_run(run_dir, [sim("1", 1.0)], "resume_x.json")
        merged = load_simulations(run_dir)
        assert len(merged) == 2
        assert all(is_scored(s) for s in merged)
        assert unscored_task_ids(run_dir) == []

    def test_a_still_failing_resume_does_not_hide_the_gap(self, run_dir: Path) -> None:
        write_run(run_dir, [sim("0", 1.0), sim("1", None)], "part1.json")
        write_run(run_dir, [sim("1", None)], "resume_x.json")
        assert unscored_task_ids(run_dir) == ["1"]

    def test_different_trials_are_kept_apart(self, run_dir: Path) -> None:
        """Repeated trials legitimately produce the same task more than once, and those
        must not be deduplicated as resumes."""
        a, b = sim("0", 1.0), sim("0", 0.0)
        b["trial"] = 1
        write_run(run_dir, [a, b])
        assert len(load_simulations(run_dir)) == 2


class TestResumeRefusesADifferentStack:
    """Resumed tasks must run on the original stack, or merging mixes two stacks."""

    @pytest.fixture
    def settings(self, monkeypatch: pytest.MonkeyPatch) -> Settings:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-eval-test-key-000000000000")
        return Settings(_env_file=None)  # type: ignore[call-arg]

    def _described(self, settings: Settings) -> dict[str, Any]:
        return EvalStack(settings=settings, agent_model="deepseek-v4-pro").describe()

    def test_identical_stack_is_accepted(self, settings: Settings) -> None:
        described = self._described(settings)
        assert rebuild_stack(settings, described).agent_model == "deepseek-v4-pro"

    def test_changed_user_simulator_is_refused(self, settings: Settings) -> None:
        """Someone changes FROZEN_USER_SIMULATOR and then resumes an older run.

        Not hypothetical: the simulated user changed from qwen3.6-plus to glm-5.3-flash
        on 2026-09-07, and the pre-switch run directories are still in the repository.
        """
        described = self._described(settings) | {"user_simulator": "qwen3.6-plus-2026-04-02"}
        with pytest.raises(SystemExit, match="differs from the original run"):
            rebuild_stack(settings, described)

    def test_replayed_run_params_are_not_treated_as_drift(self, settings: Settings) -> None:
        """The thinking level was the caller's choice and a resume copies it; verifying
        it would be comparing a value with itself."""
        described = self._described(settings) | {"agent_thinking_effort": "low"}
        assert rebuild_stack(settings, described).agent_thinking_effort == "low"

    def test_changed_judge_is_refused(self, settings: Settings) -> None:
        """A different judge is a different ruler, which is why 0.800 and 0.600 from two
        rounds were not comparable."""
        described = self._described(settings) | {"nl_judge": "some-other-judge"}
        with pytest.raises(SystemExit, match="differs from the original run"):
            rebuild_stack(settings, described)


class TestResumeKnowsWhichSubjectItIsResuming:
    """A resume must use the same subject under test.

    Upstream's agent and the guarded agent are different subjects whose scores are not
    comparable. Resuming with the other one mixes both into one directory, and the
    merged average looks entirely normal, which is the adulteration resume_run exists to
    prevent.

    This happened: when `--guarded` was added, summary.json did not record the subject at
    all, and resume_run would silently have used the upstream agent to fill a guarded run.
    """

    def _resolve(self, name: str, summary: dict[str, Any]) -> Any:
        """pytest's tmp_path has permission problems on this machine; use
        TemporaryDirectory."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from resume_run import resolve_subject

        with tempfile.TemporaryDirectory(prefix="rsa_subject_") as d:
            run_dir = Path(d) / name
            run_dir.mkdir()
            return resolve_subject(run_dir, summary)

    def test_guarded_run_resumes_guarded(self) -> None:
        assert self._resolve("r_guarded", {"agent_under_test": "guarded"}) is True

    def test_upstream_run_resumes_upstream(self) -> None:
        assert self._resolve("r_upstream", {"agent_under_test": "upstream"}) is False

    def test_pre_m06_run_without_the_field_is_upstream(self) -> None:
        """Before M0.6 there was only the upstream subject, so a missing field means
        upstream."""
        assert self._resolve("20260907T080504Z_x_holdout", {}) is False

    def test_missing_field_on_a_guarded_looking_dir_is_refused(self) -> None:
        """The directory name says guarded while the field is missing: two signals
        disagree, so abort rather than guess."""
        with pytest.raises(SystemExit, match="cannot be confirmed"):
            self._resolve("r_guarded", {})

    def test_contradiction_between_name_and_record_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="disagree"):
            self._resolve("r_upstream", {"agent_under_test": "guarded"})
