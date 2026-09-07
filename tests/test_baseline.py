"""`baseline-v1` 的完整性。

基线的用途是**给后续每一次改动一个可比对象**。所以这里守的不是"数字好不好看"，
而是三件事：它没有被悄悄改过、它记的分母是对的、它自称完整就必须真的完整。

这些断言全部只读入库文件，不需要 bench 依赖，也不需要原始逐题轨迹
（那些按 .gitignore 不入库，CI 上本来就没有）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
BASELINE_DIR = REPO / "evaluation" / "baselines" / "baseline-v1"
BASELINE_JSON = BASELINE_DIR / "baseline.json"
CHECKSUMS = BASELINE_DIR / "checksums.sha256"

# 与 scripts/freeze_baseline.py 里的常量一致。写死在测试里是有意的：
# 两边都改才算换基线，单边改动会在这里变红。
BASELINE_RUN = "20260907T080504Z_deepseek-v4-pro_holdout"
UPSTREAM_COMMIT = "fc0055dc4e0a316c3f83133267fbd6faaa770992"


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    if not BASELINE_JSON.is_file():
        pytest.fail(f"没有基线文件：{BASELINE_JSON}。运行 scripts/freeze_baseline.py 生成。")
    loaded: dict[str, Any] = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
    return loaded


def _parse_checksums() -> tuple[dict[str, str], dict[str, str]]:
    tracked: dict[str, str] = {}
    local: dict[str, str] = {}
    current = tracked
    for line in CHECKSUMS.read_text(encoding="utf-8").splitlines():
        if line.startswith("# tracked"):
            current = tracked
        elif line.startswith("# local_only"):
            current = local
        elif line.strip():
            digest, name = line.split("  ", 1)
            current[name] = digest
    return tracked, local


class TestIntegrity:
    def test_tracked_files_match_their_recorded_digests(self) -> None:
        """基线依赖的入库文件一个都不能变。

        变了就说明基线的前提被动过——任务集换了、上游 manifest 改了、
        或者那一轮的 summary 被编辑过。此时基线不再代表它自称的那次运行。
        """
        tracked, _ = _parse_checksums()
        assert tracked, "tracked 段是空的，校验和文件形同虚设"
        problems = []
        for name, digest in tracked.items():
            path = REPO / name
            if not path.is_file():
                problems.append(f"缺失：{name}")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != digest:
                problems.append(f"内容已变：{name}")
        assert not problems, (
            "基线依赖的文件与冻结时不一致：\n  "
            + "\n  ".join(problems)
            + "\n若确属有意换基线，请重新运行 scripts/freeze_baseline.py 并在 PR 说明原因。"
        )

    def test_raw_traces_are_recorded_but_not_required(self) -> None:
        """原始轨迹按 .gitignore 不入库，所以只记摘要、不要求存在。

        混进 tracked 段会让 CI 因为文件本来就不该在那里而永远红。
        """
        _, local = _parse_checksums()
        assert local, "一条原始轨迹的摘要都没记——那这一轮的原始数据无从校验"
        for name in local:
            assert "/results.json" in name, f"local_only 里出现了不像原始轨迹的条目：{name}"


class TestTheNumbers:
    def test_denominator_is_task_count_not_record_count(self, baseline: dict[str, Any]) -> None:
        """分母必须是**题数**，不是模拟记录条数。

        补跑会让同一道题留下两条记录（失败的那条和补跑成功的那条）。
        本项目已经踩过一次：每题成本按 45 条记录算，而实际只有 40 题，
        于是对外报了一个偏低 11% 的成本。
        """
        run = baseline["run"]
        assert run["expected_simulations"] == run["task_count"] * run["num_trials"]
        assert run["scored_simulations"] == run["expected_simulations"]
        # 有补跑，才更要盯住分母
        assert run["resumed_task_ids"], "这一轮确实有补跑，记录里不该是空的"

    def test_baseline_run_is_complete(self, baseline: dict[str, Any]) -> None:
        """自称完整就必须真的完整：缺题会让平均分虚高。"""
        run = baseline["run"]
        assert run["complete"] is True
        assert run["scored_simulations"] == 40

    def test_reward_matches_pass_count(self, baseline: dict[str, Any]) -> None:
        """奖励是全或无，所以平均分必须恰好等于满分题数除以题数。

        对不上说明有 0/1 以外的分数混进来了，那时"满分题数"这个说法就不成立。
        """
        run = baseline["run"]
        assert run["avg_reward"] == pytest.approx(run["pass_count"] / run["scored_simulations"])
        assert run["avg_reward"] == pytest.approx(0.875)
        assert run["pass_count"] == 35


class TestProvenance:
    def test_it_pins_the_same_upstream_commit_as_the_manifest(
        self, baseline: dict[str, Any]
    ) -> None:
        """基线与 τ²-bench 的固定 commit 必须一致，否则成绩不可比。"""
        manifest = json.loads(
            (REPO / "evaluation" / "tau2_manifest.json").read_text(encoding="utf-8")
        )
        assert baseline["upstream"]["commit"] == UPSTREAM_COMMIT
        assert manifest["upstream"]["commit"] == UPSTREAM_COMMIT

    def test_it_names_the_run_it_came_from(self, baseline: dict[str, Any]) -> None:
        assert baseline["run"]["run_dir"] == BASELINE_RUN

    def test_it_records_the_stack_that_produced_it(self, baseline: dict[str, Any]) -> None:
        """光有分数没有评测栈，这个数字无法解释也无法复现。"""
        stack = baseline["run"]["stack"]
        for key in ("agent_model", "user_simulator", "nl_judge", "agent_thinking_effort"):
            assert stack.get(key), f"基线没记 {key}，这个分数就没法复现"

    def test_cost_is_not_frozen_into_the_baseline(self, baseline: dict[str, Any]) -> None:
        """成本刻意不冻结：价格快照会新增条目，抄进来会悄悄过期。

        成本以 scripts/analyze_eval.py 现算为准（口径见 M0.5（一））。
        """
        assert "cost" not in json.dumps(baseline["run"])
        assert "analyze_eval" in baseline["note"]
