"""Run the MCP server's acceptance table and write the numbers to a JSON report.

    uv run python scripts/run_mcp_acceptance.py
    uv run python scripts/run_mcp_acceptance.py --out reports/mcp_acceptance/ci.json

The table is the one in docs/M9_MCP_SERVER.md: ten scenarios, each run in the 2026-07-28
protocol and in the legacy handshake, over real Streamable HTTP. No model is called and
nothing is billed. `tests/test_mcp_acceptance.py` runs the same scenarios and fails the
build if one stops passing.

Needs a PostgreSQL it may write to, from `--database-url` or `RSA_TEST_DATABASE_URL`.
`RSA_DATABASE_URL` is deliberately not read: the run provisions identities and writes
operations, and it should not land in the demo's database by default. Tables are created
if missing and never dropped. Every scenario uses fresh subjects, so runs do not
interfere with each other or with anything already there.

Exit status is 1 when any scenario fails.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from retail_support_agent.db import Base, make_engine, make_session_factory  # noqa: E402
from retail_support_agent.mcp_acceptance import MODES, run_all  # noqa: E402
from retail_support_agent.store import Store  # noqa: E402

DEFAULT_DIR = REPO_ROOT / "reports" / "mcp_acceptance"


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def provenance() -> dict[str, Any]:
    """What produced the numbers: without it a report cannot be traced to a revision."""
    return {
        "commit": _git("rev-parse", "HEAD"),
        "working_tree_clean": _git("status", "--porcelain") == "",
        "mcp_sdk": importlib.metadata.version("mcp"),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("RSA_TEST_DATABASE_URL"))
    parser.add_argument("--out", type=Path, default=None, help="report path")
    args = parser.parse_args()
    if not args.database_url:
        print("set --database-url or RSA_TEST_DATABASE_URL to a database this may write to")
        return 2

    engine = make_engine(args.database_url)
    Base.metadata.create_all(engine)
    store = Store(make_session_factory(engine))

    started = dt.datetime.now(dt.UTC)
    body = asyncio.run(run_all(store, MODES))
    finished = dt.datetime.now(dt.UTC)
    engine.dispose()

    report = {
        "report": "mcp_acceptance",
        "plan": "docs/M9_MCP_SERVER.md",
        "date": started.date().isoformat(),
        "started_at": started.isoformat(timespec="seconds"),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "billed_model_calls": 0,
        "provenance": provenance(),
        **body,
    }

    for name, by_mode in body["scenarios"].items():
        marks = "  ".join(
            f"{mode}: {'pass' if run['passed'] else 'FAIL'}" for mode, run in by_mode.items()
        )
        print(f"  {name:<44} {marks}")
    print(f"\n{'all passed' if body['passed'] else 'FAILED'}")

    out = args.out or DEFAULT_DIR / f"{report['date']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report written to {out}")
    return 0 if body["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
