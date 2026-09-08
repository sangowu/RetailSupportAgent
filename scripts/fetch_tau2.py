"""Fetch τ²-bench at the commit pinned by the manifest and verify its integrity.

Upstream code and data are not committed to this repository, which keeps their MIT
attribution intact and avoids presenting upstream artifacts as our own.

Usage:
    uv run python scripts/fetch_tau2.py            # fetch and verify
    uv run python scripts/fetch_tau2.py --verify-only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retail_support_agent.tau2_pin import (
    DEFAULT_CHECKOUT,
    ManifestError,
    load_manifest,
    verify_checkout,
)


def _force_utf8_output() -> None:
    """Windows consoles default to a code page that mangles non-ASCII output."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def run(cmd: list[str], cwd: Path | None = None) -> None:
    """Run a subprocess and abort on failure, without swallowing upstream's message."""
    subprocess.run(cmd, cwd=cwd, check=True)


# Line-ending conversion is disabled: Windows autocrlf would rewrite upstream files to
# CRLF, so the same commit would hash differently per platform and the digest pin would
# stop holding.
LF_CONFIG = ["-c", "core.autocrlf=false", "-c", "core.eol=lf"]


def fetch(checkout: Path) -> None:
    """Fetch the pinned commit. An existing checkout is moved to that commit, never
    updated implicitly."""
    manifest = load_manifest()
    repo = manifest["upstream"]["repo"]
    commit = manifest["upstream"]["commit"]

    if not (checkout / ".git").is_dir():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        run(["git", *LF_CONFIG, "clone", "--filter=blob:none", repo, str(checkout)])
    # An existing checkout may have been cloned with conversion on, so the setting is
    # written into the repository itself.
    run(["git", "config", "core.autocrlf", "false"], cwd=checkout)
    run(["git", "config", "core.eol", "lf"], cwd=checkout)
    run(["git", "fetch", "--quiet", "origin", commit], cwd=checkout)
    run(["git", "checkout", "--quiet", "--detach", commit], cwd=checkout)
    # The index may cache converted content, so files are written out again under the
    # new setting.
    run(["git", "rm", "--cached", "-r", "-q", "."], cwd=checkout)
    run(["git", "reset", "--hard", "--quiet"], cwd=checkout)
    print(f"checked out {repo} @ {commit}")


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(description="Fetch and verify the pinned τ²-bench")
    parser.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--verify-only", action="store_true", help="verify without fetching")
    args = parser.parse_args()

    try:
        if not args.verify_only:
            fetch(args.checkout)
        mismatches = verify_checkout(args.checkout)
    except (ManifestError, subprocess.CalledProcessError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 2

    if mismatches:
        print(
            f"verification failed: {len(mismatches)} files differ from the manifest:",
            file=sys.stderr,
        )
        for item in mismatches:
            print(item.describe(), file=sys.stderr)
        return 1

    manifest = load_manifest()
    print(
        "verified: "
        f"{len(manifest['files_sha256'])} files match "
        f"{manifest['upstream']['tag']} ({manifest['upstream']['commit'][:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
