"""Read and verify the pinned τ²-bench upstream.

M0.1 makes "which version of the benchmark was evaluated" a machine-checkable fact:
the manifest records the upstream commit and a SHA256 per file, so any local edit or
upstream drift shows up during verification.
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
    """The manifest is missing, corrupt, or disagrees with the checkout."""


@dataclass(frozen=True)
class FileMismatch:
    """One file that does not match the manifest."""

    path: str
    expected: str
    actual: str | None  # None means the file is missing

    def describe(self) -> str:
        actual = "<missing>" if self.actual is None else self.actual
        return f"{self.path}\n  expected {self.expected}\n  actual   {actual}"


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Read the manifest. Raises ManifestError when missing or not valid JSON."""
    if not manifest_path.is_file():
        raise ManifestError(f"manifest not found: {manifest_path}")
    try:
        data: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupt file is an error path
        raise ManifestError(f"manifest is not valid JSON: {manifest_path}") from exc
    return data


def sha256_of(path: Path) -> str:
    """SHA256 of a file, read in chunks so large files such as db.json are not
    loaded into memory whole."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checkout(
    checkout: Path = DEFAULT_CHECKOUT,
    manifest_path: Path = MANIFEST_PATH,
) -> list[FileMismatch]:
    """Verify the local checkout against the manifest. An empty list means it passes."""
    manifest = load_manifest(manifest_path)
    expected: dict[str, str] = manifest["files_sha256"]
    if not checkout.is_dir():
        raise ManifestError(
            f"upstream checkout not found: {checkout} (run scripts/fetch_tau2.py first)"
        )

    mismatches: list[FileMismatch] = []
    for rel, expected_hash in sorted(expected.items()):
        target = checkout / rel
        actual = sha256_of(target) if target.is_file() else None
        if actual != expected_hash:
            mismatches.append(FileMismatch(rel, expected_hash, actual))
    return mismatches


def load_task_set(name: str) -> dict[str, Any]:
    """Read a frozen task set (dev / holdout / regression / scope_labels)."""
    path = TASK_SETS_DIR / f"{name}.json"
    if not path.is_file():
        raise ManifestError(f"task set not found: {path}")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data
