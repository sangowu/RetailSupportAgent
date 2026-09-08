"""Read the raw results of an evaluation run.

A separate module for two reasons:

1. Upstream treats `save_to` as a directory and writes `<save_to>/results.json`. That
   surprising behaviour should not be re-implemented in every script.
2. One run can be written in several parts. Early stopping splits 12 tasks into "run 4
   first" and "run the remaining 8"; resuming after a provider failure adds another
   part. Reading must merge the parts into one run and count each task once.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def result_files(run_dir: Path) -> list[Path]:
    """Every part file of one run, sorted by name for a stable order."""
    found: list[Path] = []
    direct = run_dir / "results.json"
    if direct.is_file():
        found.append(direct)
    found.extend(sorted(p / "results.json" for p in sorted(run_dir.iterdir()) if p.is_dir()))
    return [p for p in found if p.is_file()]


def is_scored(simulation: dict[str, Any]) -> bool:
    """Whether this record has a reward.

    The usual reason for none is a provider failure
    (`termination_reason=infrastructure_error`), where `messages` is often empty
    because the task never started.
    """
    return (simulation.get("reward_info") or {}).get("reward") is not None


def load_simulations(run_dir: Path) -> list[dict[str, Any]]:
    """Merge all simulation records of a run, keeping one per task.

    Resuming makes a task_id appear twice: the earlier failed record with no reward,
    and the successful retry. Counting both inflates the simulation count and keeps
    "unscored tasks" from ever reaching zero, so a scored record replaces an unscored
    one, and between two scored records the first wins, part order being execution
    order.
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in result_files(run_dir):
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        for sim in data.get("simulations") or []:
            key = f"{sim.get('task_id')}#{sim.get('trial', 0)}"
            previous = merged.get(key)
            if previous is None or (not is_scored(previous) and is_scored(sim)):
                merged[key] = sim
    return list(merged.values())
