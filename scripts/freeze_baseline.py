"""Freeze and verify baselines: the authoritative baseline records and checksums.

A baseline exists to give every later change something to compare against, so it must
be generated reproducibly rather than transcribed into Markdown by hand. Transcribed
numbers drift away from the code, and this project has already been caught by it once,
computing per-task cost over 45 records when the run had 40 tasks.

What is produced:

    evaluation/baselines/<id>/baseline.json     the record, with every number computed
    evaluation/baselines/<id>/checksums.sha256  digests of the files it depends on

Checksums come in two kinds, and the distinction matters:

  tracked  committed files (summary.json, task sets, the upstream manifest). Anyone who
           clones the repository can verify them, and CI does.
  local    raw per-task traces (about 100KB per task, gitignored). Only the machine that
           ran the evaluation has them. Their digests are recorded, but a missing file is
           not a failure during verification, only a note that it is not present here.
           Mixing the two would keep CI permanently red over files that are not supposed
           to exist there.

Usage:
    uv run python scripts/freeze_baseline.py            # create or update
    uv run python scripts/freeze_baseline.py --verify   # verify only, write nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from retail_support_agent.pricing import PRICE_SNAPSHOT_DATE  # noqa: E402
from retail_support_agent.results_io import load_simulations  # noqa: E402

BASELINES_ROOT = REPO / "evaluation" / "baselines"
RESULTS_ROOT = REPO / "evaluation" / "results"

# The baseline registry. Run directories are written out rather than resolved as "the
# latest run": a baseline exists precisely because it does not follow the latest run. To
# change a baseline, add an entry here and explain why in the PR.
#
# Old baselines are archived, never deleted. Deleting one makes the number cited in a
# historical report unverifiable; keeping it and continuing to check its checksums means
# every CI run confirms that the original record has not been altered.
BASELINES: dict[str, dict[str, str]] = {
    "baseline-v1": {
        "run": "20260907T080504Z_deepseek-v4-pro_holdout",
        "split": "holdout",
        "status": "archived",
        "subject": "upstream",
        "scope_note": (
            "The 40-task holdout. Not a standard τ²-bench Retail score but a subset "
            "score; its value is the property of being an independent verification "
            "never used for tuning, so it is archived rather than deleted."
        ),
    },
    "baseline-v2": {
        "run": "20260907T205640Z_deepseek-v4-pro_full",
        "split": "full",
        "status": "reference",
        "subject": "upstream",
        "scope_note": (
            "All 114 tasks, which may be called a standard τ²-bench Retail score. It "
            "includes the 74 dev tasks, some of which were used for tuning, so it is a "
            "standard-scope score rather than an independent verification; the two are "
            "not interchangeable. The subject under test is upstream's llm_agent, not "
            "this project's agent. It now serves as the control for baseline-v3 and is "
            "kept and verified."
        ),
    },
    "baseline-v3": {
        "run": "20260908T121800Z_deepseek-v4-pro_full_guarded",
        "split": "full",
        "status": "current",
        "subject": "guarded",
        "scope_note": (
            "All 114 tasks, with this project's write-guarded agent as the subject under "
            "test. Same stack, model and seed as baseline-v2, so the two can be paired "
            "task by task. They are different subjects: their scores are presented side "
            "by side and never averaged together."
        ),
    },
}
CURRENT_BASELINE = "baseline-v3"


def baseline_dir(baseline_id: str) -> Path:
    return BASELINES_ROOT / baseline_id


# Committed files a baseline depends on. A missing entry means one of its premises is
# not pinned.
TRACKED_DEPS = (
    "evaluation/tau2_manifest.json",
    "evaluation/task_sets/dev.json",
    "evaluation/task_sets/full.json",
    "evaluation/task_sets/holdout.json",
    "evaluation/task_sets/regression.json",
    "evaluation/task_sets/scope_labels.json",
    "evaluation/schemas/run_manifest.schema.json",
    "evaluation/schemas/task_result.schema.json",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> dict[str, Any]:
    """Record the state of the code. Whether the working tree is dirty must be recorded:
    a baseline produced on uncommitted changes cannot be reproduced by anyone else, and
    the commit hash alone does not reveal that.

    The baseline directory itself does not count: freezing necessarily writes
    baseline.json and checksums.sha256, so including them would make dirty permanently
    true, and an always-true warning is no warning at all.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    rel = BASELINES_ROOT.relative_to(REPO).as_posix()
    others = [line for line in status if rel not in line.replace("\\", "/")]
    return {"commit": head, "dirty": bool(others), "dirty_excludes": rel}


def upstream_pin() -> dict[str, Any]:
    manifest = json.loads((REPO / "evaluation" / "tau2_manifest.json").read_text(encoding="utf-8"))
    upstream = manifest.get("upstream") or {}
    return {
        "repo": upstream.get("repo"),
        "tag": upstream.get("tag"),
        "commit": upstream.get("commit"),
        "license": upstream.get("license"),
        # The per-file digests live in tau2_manifest.json; only the count is recorded
        # here. That file's integrity is covered by the tracked section of
        # checksums.sha256.
        "pinned_file_count": len(manifest.get("files_sha256") or {}),
    }


def raw_trace_files(run_dir: Path) -> list[Path]:
    """Raw per-task traces of this run. Upstream treats save_to as a directory, so they
    have the shape `<name>.json/results.json`."""
    found = [p for p in run_dir.glob("*.json/results.json") if p.is_file()]
    return sorted(found)


def build_record(run_dir: Path, split: str) -> dict[str, Any]:
    """Compute the baseline record from the raw results. No number is copied from a
    document."""
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    simulations = load_simulations(run_dir)

    rewards: list[float] = [
        float(reward)
        for sim in simulations
        if (reward := (sim.get("reward_info") or {}).get("reward")) is not None
    ]
    scored = len(rewards)
    passed = sum(1 for r in rewards if r == 1.0)

    task_ids = list(summary.get("task_ids") or [])
    # The denominator is the task count, not the number of simulation records. Resuming
    # leaves two records for one task, the failed attempt and the successful retry.
    # Computing per-task cost over 45 records for a 40-task run came from exactly this.
    expected = len(task_ids) * int(summary.get("num_trials", 1) or 1)

    return {
        "run_dir": run_dir.name,
        "run_id": summary.get("run_id"),
        "run_at": summary.get("run_at"),
        # The subject under test must be recorded: upstream's llm_agent and the guarded
        # agent are different subjects whose scores are not comparable. Runs before M0.6
        # have no such field and are treated as upstream.
        "agent_under_test": summary.get("agent_under_test", "upstream"),
        "task_split": split,
        "task_count": len(task_ids),
        "num_trials": int(summary.get("num_trials", 1) or 1),
        "expected_simulations": expected,
        "scored_simulations": scored,
        "complete": scored == expected,
        "avg_reward": (sum(rewards) / scored) if scored else None,
        "pass_count": passed,
        "stack": summary.get("stack"),
        "resumed_task_ids": summary.get("resumed_task_ids") or [],
    }


def collect_checksums(run_dir: Path) -> dict[str, dict[str, str]]:
    tracked: dict[str, str] = {}
    for rel in TRACKED_DEPS:
        path = REPO / rel
        if not path.is_file():
            raise SystemExit(f"a committed file the baseline depends on is missing: {rel}")
        tracked[rel] = sha256_of(path)
    for name in ("summary.json", "resume_summary.json"):
        path = run_dir / name
        if path.is_file():
            tracked[path.relative_to(REPO).as_posix()] = sha256_of(path)

    local = {p.relative_to(REPO).as_posix(): sha256_of(p) for p in raw_trace_files(run_dir)}
    return {"tracked": tracked, "local_only": local}


def write_baseline(
    baseline_id: str, record: dict[str, Any], checksums: dict[str, dict[str, str]]
) -> None:
    entry = BASELINES[baseline_id]
    out_dir = baseline_dir(baseline_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline = {
        "baseline_id": baseline_id,
        # The scope is written into the baseline: subset and full-set scores are both
        # real numbers but must not stand in for each other.
        "status": entry["status"],
        "task_split": entry["split"],
        "scope_note": entry["scope_note"],
        "frozen_at": datetime.now(UTC).isoformat(),
        "code": git_commit(),
        "upstream": upstream_pin(),
        "price_snapshot_date": PRICE_SNAPSHOT_DATE,
        "run": record,
        "checksum_file": "checksums.sha256",
        "note": (
            "Cost comes from scripts/analyze_eval.py and is deliberately not frozen "
            "here: the price snapshot gains entries over time, and a cost copied into "
            "the baseline would expire silently."
        ),
    }
    (out_dir / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    lines = ["# tracked: committed files, verifiable from any clone (CI checks this section)"]
    lines += [f"{digest}  {path}" for path, digest in sorted(checksums["tracked"].items())]
    lines.append("# local_only: raw per-task traces, gitignored; a missing file is not a failure")
    lines += [f"{digest}  {path}" for path, digest in sorted(checksums["local_only"].items())]
    (out_dir / "checksums.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def parse_checksums(baseline_id: str) -> dict[str, dict[str, str]]:
    path = baseline_dir(baseline_id) / "checksums.sha256"
    if not path.is_file():
        raise SystemExit(
            f"no checksum file at {path}. Run scripts/freeze_baseline.py --id {baseline_id} first"
        )
    section = "tracked"
    out: dict[str, dict[str, str]] = {"tracked": {}, "local_only": {}}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# tracked"):
            section = "tracked"
        elif line.startswith("# local_only"):
            section = "local_only"
        elif line.strip():
            digest, name = line.split("  ", 1)
            out[section][name] = digest
    return out


def verify_one(baseline_id: str) -> int:
    expected = parse_checksums(baseline_id)
    bad: list[str] = []
    missing_local: list[str] = []

    for name, digest in expected["tracked"].items():
        path = REPO / name
        if not path.is_file():
            bad.append(f"missing (a committed file should be here): {name}")
        elif sha256_of(path) != digest:
            bad.append(f"content changed: {name}")

    for name, digest in expected["local_only"].items():
        path = REPO / name
        if not path.is_file():
            missing_local.append(name)
        elif sha256_of(path) != digest:
            bad.append(f"raw trace changed: {name}")

    status = BASELINES[baseline_id]["status"]
    print(
        f"[{baseline_id}] ({status}) {len(expected['tracked'])} committed files, "
        f"{len(expected['local_only'])} raw traces"
    )
    if missing_local:
        print(
            f"  {len(missing_local)} raw traces are not on this machine, which is by "
            "design and not a failure"
        )
    if bad:
        print("  verification failed:")
        for item in bad:
            print(f"    {item}")
        return 1
    print("  verified.")
    return 0


def verify_all() -> int:
    """Verify every baseline, archived ones included.

    Continuing to verify archived baselines is what makes "the number in that historical
    report is still traceable" true. Verifying only the current one would let an old
    baseline be altered unnoticed.
    """
    failures = [bid for bid in BASELINES if verify_one(bid) != 0]
    if failures:
        print(
            "\nthese baselines have been altered: "
            + ", ".join(failures)
            + ". If that was deliberate, re-freeze them and explain the change in the PR."
        )
        return 1
    print(f"\nall {len(BASELINES)} baselines verified.")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Freeze or verify baselines")
    parser.add_argument(
        "--verify", action="store_true", help="verify every baseline and write nothing"
    )
    parser.add_argument(
        "--id", default=CURRENT_BASELINE, choices=sorted(BASELINES), help="which baseline to freeze"
    )
    parser.add_argument(
        "--run",
        default=None,
        help="run directory name, needed only when the baseline has none bound yet; "
        "the binding is then written into the registry",
    )
    args = parser.parse_args()

    if args.verify:
        return verify_all()

    entry = BASELINES[args.id]
    run_name = args.run or entry["run"]
    if not run_name:
        raise SystemExit(
            f"{args.id} has no run directory bound. For a first freeze, name it explicitly: "
            f"scripts/freeze_baseline.py --id {args.id} --run <run directory>"
        )
    if entry["run"] and args.run and args.run != entry["run"]:
        raise SystemExit(
            f"{args.id} is bound to {entry['run']}, which differs from --run {args.run}. "
            "Changing the run directory means changing the baseline: edit the BASELINES "
            "registry and explain why in the PR."
        )
    run_dir = RESULTS_ROOT / run_name
    if not run_dir.is_dir():
        raise SystemExit(f"baseline run directory not found: {run_dir}")

    record = build_record(run_dir, entry["split"])
    if not record["complete"]:
        raise SystemExit(
            f"this run is incomplete ({record['scored_simulations']}/"
            f"{record['expected_simulations']} scored) and cannot be a baseline: missing "
            "tasks inflate the average. Complete it with scripts/resume_run.py first."
        )

    checksums = collect_checksums(run_dir)
    write_baseline(args.id, record, checksums)
    print(f"froze {args.id}: {baseline_dir(args.id).relative_to(REPO).as_posix()}")
    print(f"  scope          {entry['split']} ({entry['status']})")
    print(f"  run directory  {record['run_dir']}")
    print(
        f"  average reward {record['avg_reward']:.4f} "
        f"({record['pass_count']}/{record['scored_simulations']})"
    )
    print(f"  committed files {len(checksums['tracked'])} digests recorded")
    print(f"  raw traces      {len(checksums['local_only'])} digests recorded (not committed)")
    if git_commit()["dirty"]:
        print(
            "\n[note] the working tree has uncommitted changes, recorded as code.dirty "
            "in baseline.json."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
