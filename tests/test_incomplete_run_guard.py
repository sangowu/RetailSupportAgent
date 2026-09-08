"""丢题必须被发现，且不能算出一个更好看的平均分。

这是实测过的缺陷，不是假想：2026-09-07 一轮 40 题 holdout 有 5 题因服务端额度
耗尽而未开始，上游把这 5 题从分母里剔掉，`avg_reward` 显示 0.886——
而补齐之后的真实成绩是 0.875。**丢题让成绩变好看**，比直接崩溃危险得多。
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
    """summarize() 读的是对象属性，不是 dict。"""

    def __init__(self, task_id: str, reward: float | None, reason: str) -> None:
        self.task_id = task_id
        self.termination_reason = reason
        self.reward_info = type("RI", (), {"reward": reward})() if reward is not None else None
        self.agent_cost = None


@pytest.fixture
def run_dir() -> Iterator[Path]:
    """自建临时目录，不用 pytest 的 tmp_path。

    本机 `Temp/pytest-of-*` 是历史遗留、权限损坏且删不掉，tmp_path 会直接
    PermissionError。这与被测逻辑无关，换个目录即可。
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
        """avg_reward 本身不改口径——改的是"它旁边必须有 incomplete 标记"。

        悄悄把分母换成 expected 会更糟：那等于把没跑的题按 0 分计，
        同样是编造。正确的做法是拒绝把这个数当成绩。
        """
        sims = [FakeSim("0", 1.0, "user_stop"), FakeSim("1", None, "infrastructure_error")]
        out = summarize(sims, expected=2)
        assert out["avg_reward"] == 1.0
        assert out["incomplete"] is True


class TestResumedTasksAreDeduplicated:
    def test_scored_record_replaces_the_failed_one(self, run_dir: Path) -> None:
        """补跑会让同一题出现两次，两条都算会让模拟数虚增、缺口永远归不了零。"""
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
        """多轮重复实验里同一题会合法地出现多次，不能被当成补跑去重掉。"""
        a, b = sim("0", 1.0), sim("0", 0.0)
        b["trial"] = 1
        write_run(run_dir, [a, b])
        assert len(load_simulations(run_dir)) == 2


class TestResumeRefusesADifferentStack:
    """补跑的题必须与原轮同栈，否则合并回去是两个栈的混合。"""

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
        """有人改了 FROZEN_USER_SIMULATOR 之后来补跑旧运行——必须拦下。

        这不是假想：模拟用户 2026-09-07 刚从 qwen3.6-plus 换成 glm-5.3-flash，
        换型前的运行目录还都在仓库里。
        """
        described = self._described(settings) | {"user_simulator": "qwen3.6-plus-2026-04-02"}
        with pytest.raises(SystemExit, match="评测栈与原轮不一致"):
            rebuild_stack(settings, described)

    def test_replayed_run_params_are_not_treated_as_drift(self, settings: Settings) -> None:
        """思考档位是调用方当时选的，补跑照抄就行——核对它等于自己跟自己比。"""
        described = self._described(settings) | {"agent_thinking_effort": "low"}
        assert rebuild_stack(settings, described).agent_thinking_effort == "low"

    def test_changed_judge_is_refused(self, settings: Settings) -> None:
        """判官换了就是换了尺子——这正是 A 轮 0.800 vs 0.600 不可比的原因。"""
        described = self._described(settings) | {"nl_judge": "some-other-judge"}
        with pytest.raises(SystemExit, match="评测栈与原轮不一致"):
            rebuild_stack(settings, described)


class TestResumeKnowsWhichSubjectItIsResuming:
    """补跑必须用**同一个被测对象**。

    上游 agent 与带护栏的 agent 是两个不同的被测对象，成绩不可比。
    用另一个去补跑，就是把两者混进同一个目录，而合并后的平均分
    看不出任何异常——正是 resume_run 存在要防的那类掺假。

    实际踩过：`--guarded` 加进来时 summary.json 根本没记被测对象，
    resume_run 会默默用上游 agent 去补带护栏的轮次。
    """

    def _resolve(self, name: str, summary: dict[str, Any]) -> Any:
        """本机的 pytest tmp_path 有权限问题，改用 TemporaryDirectory。"""
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
        """M0.6 之前只有上游一个被测对象，缺字段按 upstream 处理。"""
        assert self._resolve("20260907T080504Z_x_holdout", {}) is False

    def test_missing_field_on_a_guarded_looking_dir_is_refused(self) -> None:
        """目录名说 guarded 而字段缺失——两个信号打架，宁可中止也不猜。"""
        with pytest.raises(SystemExit, match="无法确认被测对象"):
            self._resolve("r_guarded", {})

    def test_contradiction_between_name_and_record_is_refused(self) -> None:
        with pytest.raises(SystemExit, match="不一致"):
            self._resolve("r_upstream", {"agent_under_test": "guarded"})
