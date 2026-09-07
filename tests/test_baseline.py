"""基线的完整性。

基线的用途是**给后续每一次改动一个可比对象**。所以这里守的不是"数字好不好看"，
而是三件事：它没有被悄悄改过、它记的分母是对的、它自称完整就必须真的完整。

**留档基线同样要守。** `baseline-v1` 已被 `baseline-v2` 取代，但历史报告里引用的
那个数字必须仍然可追溯；只校验当前基线，等于默许旧基线被悄悄改动而无人察觉。

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
BASELINES_ROOT = REPO / "evaluation" / "baselines"

UPSTREAM_COMMIT = "fc0055dc4e0a316c3f83133267fbd6faaa770992"

# 与 scripts/freeze_baseline.py 的登记表一一对应。写死在测试里是有意的：
# 两边都改才算换基线，单边改动会在这里变红。
BASELINE_FACTS: dict[str, dict[str, Any]] = {
    "baseline-v1": {
        "run": "20260907T080504Z_deepseek-v4-pro_holdout",
        "split": "holdout",
        "status": "archived",
        "tasks": 40,
        "pass_count": 35,
        "avg_reward": 0.875,
    },
    "baseline-v2": {
        "run": "20260907T205640Z_deepseek-v4-pro_full",
        "split": "full",
        "status": "current",
        "tasks": 114,
        "pass_count": 97,
        "avg_reward": 97 / 114,
    },
}
ALL_IDS = sorted(BASELINE_FACTS)


def _load(baseline_id: str) -> dict[str, Any]:
    path = BASELINES_ROOT / baseline_id / "baseline.json"
    if not path.is_file():
        pytest.fail(
            f"没有基线文件：{path}。运行 scripts/freeze_baseline.py --id {baseline_id} 生成。"
        )
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _parse_checksums(baseline_id: str) -> tuple[dict[str, str], dict[str, str]]:
    tracked: dict[str, str] = {}
    local: dict[str, str] = {}
    current = tracked
    path = BASELINES_ROOT / baseline_id / "checksums.sha256"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# tracked"):
            current = tracked
        elif line.startswith("# local_only"):
            current = local
        elif line.strip():
            digest, name = line.split("  ", 1)
            current[name] = digest
    return tracked, local


@pytest.mark.parametrize("baseline_id", ALL_IDS)
class TestEachBaseline:
    def test_tracked_files_match_their_recorded_digests(self, baseline_id: str) -> None:
        """基线依赖的入库文件一个都不能变。

        变了就说明基线的前提被动过——任务集换了、上游 manifest 改了、
        或者那一轮的 summary 被编辑过。此时基线不再代表它自称的那次运行。
        """
        tracked, _ = _parse_checksums(baseline_id)
        assert tracked, "tracked 段是空的，校验和文件形同虚设"
        problems = []
        for name, digest in tracked.items():
            path = REPO / name
            if not path.is_file():
                problems.append(f"缺失：{name}")
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                problems.append(f"内容已变：{name}")
        assert not problems, (
            f"{baseline_id} 依赖的文件与冻结时不一致：\n  "
            + "\n  ".join(problems)
            + "\n若确属有意换基线，请重新冻结并在 PR 说明原因。"
        )

    def test_raw_traces_are_recorded_but_not_required(self, baseline_id: str) -> None:
        """原始轨迹按 .gitignore 不入库，所以只记摘要、不要求存在。

        混进 tracked 段会让 CI 因为文件本来就不该在那里而永远红。
        """
        _, local = _parse_checksums(baseline_id)
        assert local, "一条原始轨迹的摘要都没记——那这一轮的原始数据无从校验"
        for name in local:
            assert "/results.json" in name, f"local_only 里出现了不像原始轨迹的条目：{name}"

    def test_denominator_is_task_count_not_record_count(self, baseline_id: str) -> None:
        """分母必须是**题数**，不是模拟记录条数。

        补跑会让同一道题留下两条记录（失败的那条和补跑成功的那条）。
        本项目已经踩过一次：每题成本按 45 条记录算而实际只有 40 题，
        于是对外报了一个偏低 11% 的成本。
        """
        run = _load(baseline_id)["run"]
        assert run["expected_simulations"] == run["task_count"] * run["num_trials"]
        assert run["scored_simulations"] == run["expected_simulations"]

    def test_run_is_complete(self, baseline_id: str) -> None:
        """自称完整就必须真的完整：缺题会让平均分虚高。"""
        run = _load(baseline_id)["run"]
        assert run["complete"] is True
        assert run["scored_simulations"] == BASELINE_FACTS[baseline_id]["tasks"]

    def test_reward_matches_pass_count(self, baseline_id: str) -> None:
        """奖励是全或无，所以平均分必须恰好等于满分题数除以题数。

        对不上说明有 0/1 以外的分数混进来了，那时"满分题数"这个说法就不成立。
        """
        run = _load(baseline_id)["run"]
        assert run["avg_reward"] == pytest.approx(run["pass_count"] / run["scored_simulations"])
        facts = BASELINE_FACTS[baseline_id]
        if facts["avg_reward"] is not None:
            assert run["avg_reward"] == pytest.approx(facts["avg_reward"])
            assert run["pass_count"] == facts["pass_count"]

    def test_it_pins_the_same_upstream_commit_as_the_manifest(self, baseline_id: str) -> None:
        """基线与 τ²-bench 的固定 commit 必须一致，否则成绩不可比。"""
        manifest = json.loads(
            (REPO / "evaluation" / "tau2_manifest.json").read_text(encoding="utf-8")
        )
        assert _load(baseline_id)["upstream"]["commit"] == UPSTREAM_COMMIT
        assert manifest["upstream"]["commit"] == UPSTREAM_COMMIT

    def test_it_names_the_run_and_split_it_came_from(self, baseline_id: str) -> None:
        facts = BASELINE_FACTS[baseline_id]
        baseline = _load(baseline_id)
        assert baseline["run"]["run_dir"] == facts["run"]
        assert baseline["task_split"] == facts["split"]
        assert baseline["status"] == facts["status"]

    def test_it_records_the_stack_that_produced_it(self, baseline_id: str) -> None:
        """光有分数没有评测栈，这个数字无法解释也无法复现。"""
        stack = _load(baseline_id)["run"]["stack"]
        for key in ("agent_model", "user_simulator", "nl_judge", "agent_thinking_effort"):
            assert stack.get(key), f"{baseline_id} 没记 {key}，这个分数就没法复现"

    def test_cost_is_not_frozen_into_the_baseline(self, baseline_id: str) -> None:
        """成本刻意不冻结：价格快照会新增条目，抄进来会悄悄过期。

        成本以 scripts/analyze_eval.py 现算为准（口径见 M0.5（一））。
        """
        baseline = _load(baseline_id)
        assert "cost" not in json.dumps(baseline["run"])
        assert "analyze_eval" in baseline["note"]

    def test_scope_is_stated_so_the_two_are_not_confused(self, baseline_id: str) -> None:
        """子集分与全集分都是真数字，但不能互相冒充——口径必须写在基线里。"""
        assert _load(baseline_id)["scope_note"].strip(), "没写口径说明，两个基线迟早被混着引用"


class TestTheTwoBaselinesCoexist:
    def test_exactly_one_is_current(self) -> None:
        """当前基线只能有一个，否则「和基线比」这句话没有唯一含义。"""
        current = [bid for bid in ALL_IDS if _load(bid)["status"] == "current"]
        assert current == ["baseline-v2"]

    def test_the_archived_one_is_kept_not_deleted(self) -> None:
        """留档基线不删：删了它，历史报告里引用的数字就再也无从校验。"""
        assert (BASELINES_ROOT / "baseline-v1" / "baseline.json").is_file()
        assert _load("baseline-v1")["status"] == "archived"

    def test_full_set_is_exactly_dev_plus_holdout(self) -> None:
        """全集必须真的等于 dev ∪ holdout，否则「全集分」这个说法不成立；
        而 dev 与 holdout 不得重叠，否则「独立验证」不成立。"""

        def ids(name: str) -> set[str]:
            path = REPO / "evaluation" / "task_sets" / f"{name}.json"
            return set(json.loads(path.read_text(encoding="utf-8"))["task_ids"])

        full, dev, holdout = ids("full"), ids("dev"), ids("holdout")
        assert len(full) == 114
        assert dev | holdout == full
        assert not (dev & holdout), "dev 与 holdout 重叠，独立验证就不成立"
