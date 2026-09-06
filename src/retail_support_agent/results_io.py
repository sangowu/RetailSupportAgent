"""读取评测运行的原始结果。

单独成模块的原因有两个：

1. 上游把 `save_to` 当**目录**用，结果落在 `<save_to>/results.json`，
   这个反直觉的行为不该在每个脚本里各写一遍。
2. 一次运行可能分成多段落盘——提前终止会把 12 题拆成"先跑 4 题看看"和
   "再跑剩下 8 题"两段。读的时候必须把各段合起来当成一次运行。
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


def load_simulations(run_dir: Path) -> list[dict[str, Any]]:
    """合并一次运行的全部模拟记录。分段跑出来的结果在这里重新变成一个整体。"""
    simulations: list[dict[str, Any]] = []
    for path in result_files(run_dir):
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        simulations.extend(data.get("simulations") or [])
    return simulations
