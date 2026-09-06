"""τ²-bench 上游固定信息的读取与校验。

M0.1 的作用是让"我们评测的到底是哪一版基准"成为可机器验证的事实：
manifest 记录上游 commit 与逐文件 SHA256，任何本地改动或上游漂移都会在校验时暴露。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "evaluation" / "tau2_manifest.json"
TASK_SETS_DIR = REPO_ROOT / "evaluation" / "task_sets"
DEFAULT_CHECKOUT = REPO_ROOT / "third_party" / "tau2-bench"


class ManifestError(RuntimeError):
    """manifest 缺失、损坏或与实际检出不一致。"""


@dataclass(frozen=True)
class FileMismatch:
    """单个文件的校验差异。"""

    path: str
    expected: str
    actual: str | None  # None 表示文件不存在

    def describe(self) -> str:
        actual = "<缺失>" if self.actual is None else self.actual
        return f"{self.path}\n  期望 {self.expected}\n  实际 {actual}"


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """读取 manifest；文件缺失或非法 JSON 时抛 ManifestError。"""
    if not manifest_path.is_file():
        raise ManifestError(f"manifest 不存在：{manifest_path}")
    try:
        data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - 损坏文件属异常路径
        raise ManifestError(f"manifest 不是合法 JSON：{manifest_path}") from exc
    return data


def sha256_of(path: Path) -> str:
    """按块计算文件 SHA256，避免把 db.json 这类大文件整体读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checkout(
    checkout: Path = DEFAULT_CHECKOUT,
    manifest_path: Path = MANIFEST_PATH,
) -> list[FileMismatch]:
    """校验本地检出与 manifest 是否一致，返回全部差异（空列表表示通过）。"""
    manifest = load_manifest(manifest_path)
    expected: dict[str, str] = manifest["files_sha256"]
    if not checkout.is_dir():
        raise ManifestError(f"上游检出不存在：{checkout}（先运行 scripts/fetch_tau2.py）")

    mismatches: list[FileMismatch] = []
    for rel, expected_hash in sorted(expected.items()):
        target = checkout / rel
        actual = sha256_of(target) if target.is_file() else None
        if actual != expected_hash:
            mismatches.append(FileMismatch(rel, expected_hash, actual))
    return mismatches


def load_task_set(name: str) -> dict[str, Any]:
    """读取冻结的任务集清单（dev / holdout / regression / scope_labels）。"""
    path = TASK_SETS_DIR / f"{name}.json"
    if not path.is_file():
        raise ManifestError(f"任务集不存在：{path}")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
