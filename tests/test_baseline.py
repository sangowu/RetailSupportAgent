"""Baseline integrity.

A baseline exists to give every later change something to compare against. These tests
do not check whether the numbers look good; they check three things: the baseline has
not been altered, its denominator is right, and a run claiming to be complete is
complete.

Archived baselines are checked too. `baseline-v1` has been superseded, but the number
historical reports cite must remain verifiable; checking only the current baseline
would let an old one be edited unnoticed.

All assertions read committed files only. No bench dependency, and no raw per-task
traces, which are gitignored and absent in CI by design.
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

# Mirrors the registry in scripts/freeze_baseline.py. Duplicating it here is
# deliberate: changing a baseline requires changing both, and a one-sided edit turns
# this red.
BASELINE_FACTS: dict[str, dict[str, Any]] = {
    "baseline-v1": {
        "run": "20260907T080504Z_deepseek-v4-pro_holdout",
        "split": "holdout",
        "status": "archived",
        "subject": "upstream",
        "tasks": 40,
        "pass_count": 35,
        "avg_reward": 0.875,
    },
    "baseline-v2": {
        "run": "20260907T205640Z_deepseek-v4-pro_full",
        "split": "full",
        "status": "reference",
        "subject": "upstream",
        "tasks": 114,
        "pass_count": 97,
        "avg_reward": 97 / 114,
    },
    "baseline-v3": {
        "run": "20260908T121800Z_deepseek-v4-pro_full_guarded",
        "split": "full",
        "status": "current",
        "subject": "guarded",
        "tasks": 114,
        "pass_count": 102,
        "avg_reward": 102 / 114,
    },
}
ALL_IDS = sorted(BASELINE_FACTS)


def _load(baseline_id: str) -> dict[str, Any]:
    path = BASELINES_ROOT / baseline_id / "baseline.json"
    if not path.is_file():
        pytest.fail(
            f"no baseline file at {path}. "
            f"Run scripts/freeze_baseline.py --id {baseline_id} to create it."
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
        """None of the committed files a baseline depends on may change.

        A change means one of its premises moved: the task set, the upstream manifest,
        or the run's summary was edited. The baseline then no longer represents the run
        it claims.
        """
        tracked, _ = _parse_checksums(baseline_id)
        assert tracked, "the tracked section is empty, so the checksum file verifies nothing"
        problems = []
        for name, digest in tracked.items():
            path = REPO / name
            if not path.is_file():
                problems.append(f"missing: {name}")
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                problems.append(f"content changed: {name}")
        assert not problems, (
            f"{baseline_id} depends on files that differ from freeze time:\n  "
            + "\n  ".join(problems)
            + "\nIf the baseline was replaced deliberately, re-freeze it and say why in the PR."
        )

    def test_raw_traces_are_recorded_but_not_required(self, baseline_id: str) -> None:
        """Raw traces are gitignored, so their digests are recorded but their presence
        is not required.

        Putting them in the tracked section would keep CI permanently red over files
        that are not supposed to be there.
        """
        _, local = _parse_checksums(baseline_id)
        assert local, "no raw-trace digest was recorded, so the run's raw data is unverifiable"
        for name in local:
            assert "/results.json" in name, f"local_only contains a non-trace entry: {name}"

    def test_denominator_is_task_count_not_record_count(self, baseline_id: str) -> None:
        """The denominator is the task count, not the number of simulation records.

        Resuming leaves two records for one task: the failed attempt and the successful
        retry. This project has already been caught by it once, computing per-task cost
        over 45 records when there were 40 tasks and reporting a figure 11% too low.
        """
        run = _load(baseline_id)["run"]
        assert run["expected_simulations"] == run["task_count"] * run["num_trials"]
        assert run["scored_simulations"] == run["expected_simulations"]

    def test_run_is_complete(self, baseline_id: str) -> None:
        """A run claiming completeness must be complete: missing tasks inflate the average."""
        run = _load(baseline_id)["run"]
        assert run["complete"] is True
        assert run["scored_simulations"] == BASELINE_FACTS[baseline_id]["tasks"]

    def test_reward_matches_pass_count(self, baseline_id: str) -> None:
        """Reward is all-or-nothing, so the average must equal passes over tasks exactly.

        A mismatch means a score other than 0 or 1 got in, and "pass count" would then
        mean nothing.
        """
        run = _load(baseline_id)["run"]
        assert run["avg_reward"] == pytest.approx(run["pass_count"] / run["scored_simulations"])
        facts = BASELINE_FACTS[baseline_id]
        if facts["avg_reward"] is not None:
            assert run["avg_reward"] == pytest.approx(facts["avg_reward"])
            assert run["pass_count"] == facts["pass_count"]

    def test_it_pins_the_same_upstream_commit_as_the_manifest(self, baseline_id: str) -> None:
        """The baseline and the τ²-bench pin must agree, or scores are not comparable."""
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
        """A score without its evaluation stack can neither be explained nor reproduced."""
        stack = _load(baseline_id)["run"]["stack"]
        for key in ("agent_model", "user_simulator", "nl_judge", "agent_thinking_effort"):
            assert stack.get(key), f"{baseline_id} does not record {key}, so it is irreproducible"

    def test_cost_is_not_frozen_into_the_baseline(self, baseline_id: str) -> None:
        """Cost is deliberately not frozen: the price snapshot gains entries, and a
        copied number expires silently.

        Cost comes from scripts/analyze_eval.py (convention in M0.5, part one).
        """
        baseline = _load(baseline_id)
        assert "cost" not in json.dumps(baseline["run"])
        assert "analyze_eval" in baseline["note"]

    def test_scope_is_stated_so_the_two_are_not_confused(self, baseline_id: str) -> None:
        """Subset and full-set scores are both real numbers but must not stand in for
        each other, so the scope is written into the baseline."""
        assert _load(baseline_id)["scope_note"].strip(), (
            "without a scope note the baselines eventually get cited interchangeably"
        )

    def test_it_records_which_agent_was_under_test(self, baseline_id: str) -> None:
        """The subject under test must be on record.

        Upstream `llm_agent` and this project's guarded agent are two different subjects
        and their scores are not comparable. Without this field the two baselines
        eventually get cited as "before and after" of one thing, and a merged average
        shows nothing unusual. Every M0 score was misread as this project's agent that
        way (see docs/M0_REPORT.md §1.1).
        """
        facts = BASELINE_FACTS[baseline_id]
        assert _load(baseline_id)["run"]["agent_under_test"] == facts["subject"]


class TestTheBaselinesCoexist:
    def test_exactly_one_is_current(self) -> None:
        """Only one baseline is current, or "compared to the baseline" has no single
        meaning."""
        current = [bid for bid in ALL_IDS if _load(bid)["status"] == "current"]
        assert current == ["baseline-v3"]

    def test_the_archived_one_is_kept_not_deleted(self) -> None:
        """Archived baselines are not deleted: without them the numbers in historical
        reports become unverifiable."""
        assert (BASELINES_ROOT / "baseline-v1" / "baseline.json").is_file()
        assert _load("baseline-v1")["status"] == "archived"

    def test_full_set_is_exactly_dev_plus_holdout(self) -> None:
        """The full set must equal dev union holdout, or "full-set score" is not true;
        and dev and holdout must not overlap, or "independent verification" is not
        true."""

        def ids(name: str) -> set[str]:
            path = REPO / "evaluation" / "task_sets" / f"{name}.json"
            return set(json.loads(path.read_text(encoding="utf-8"))["task_ids"])

        full, dev, holdout = ids("full"), ids("dev"), ids("holdout")
        assert len(full) == 114
        assert dev | holdout == full
        assert not (dev & holdout), "dev and holdout overlap, so the holdout is not independent"
