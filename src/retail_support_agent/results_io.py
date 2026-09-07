"""读取评测运行的原始结果。

单独成模块的原因有两个：

1. 上游把 `save_to` 当**目录**用，结果落在 `<save_to>/results.json`，
   这个反直觉的行为不该在每个脚本里各写一遍。
2. 一次运行可能分成多段落盘——提前终止会把 12 题拆成"先跑 4 题看看"和
   "再跑剩下 8 题"两段；服务端故障后补跑失败的题又会多一段。
   读的时候必须把各段合起来当成一次运行，且**同一题只算一次**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def result_files(run_dir: Path) -> list[Path]:
    """一次运行下所有分段的结果文件，按文件名排序保证顺序稳定。"""
    found: list[Path] = []
    direct = run_dir / "results.json"
    if direct.is_file():
        found.append(direct)
    found.extend(sorted(p / "results.json" for p in sorted(run_dir.iterdir()) if p.is_dir()))
    return [p for p in found if p.is_file()]


def is_scored(simulation: dict[str, Any]) -> bool:
    """这条记录拿到分数了吗。

    没拿到分的原因通常是服务端故障（`termination_reason=infrastructure_error`），
    此时 `messages` 往往是空的——题根本没开始跑。
    """
    return (simulation.get("reward_info") or {}).get("reward") is not None


def load_simulations(run_dir: Path) -> list[dict[str, Any]]:
    """合并一次运行的全部模拟记录，同一题只保留一条。

    补跑会让同一个 task_id 出现两次：先前失败的那条（无分数）和补跑成功的那条。
    两条都算会把模拟数虚增，也会让"未评分题数"永远归不了零——
    所以**有分数的覆盖没分数的**，同为有分数则保留先出现的（分段顺序即执行顺序）。
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
