"""Run the access-control evaluation set and report what held.

    uv run python scripts/run_access_eval.py
    uv run python scripts/run_access_eval.py --out evaluation/results/access-v1.json

No model is called and no money is spent: the attacker's moves are scripted, so this
runs on every commit. `tests/test_access_eval.py` runs the same cases and fails the
build if any of them stops being blocked.

Exit status is 1 when a case fails, so it can be used as a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from retail_support_agent.access_eval import (  # noqa: E402
    CASES_PATH,
    load_cases,
    run_all,
    summarize,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="case set to run")
    parser.add_argument("--out", type=Path, default=None, help="write the summary here as JSON")
    parser.add_argument("--quiet", action="store_true", help="print the summary line only")
    args = parser.parse_args()

    document = load_cases(args.cases)
    results = run_all(document)
    summary = summarize(results, str(document.get("version", "unknown")))

    if not args.quiet:
        for result in results:
            mark = "blocked" if result.blocked else "LEAKED "
            print(f"  {mark}  {result.category:<22} {result.case_id}")
            for failure in result.failures:
                print(f"            {failure}")
        print()
        for category, counts in sorted(summary["by_category"].items()):
            print(f"  {category:<22} {counts['blocked']}/{counts['attempted']}")
        print()

    print(f"{summary['blocked']}/{summary['attempted']} blocked  ({summary['version']})")

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"summary written to {args.out}")

    return 0 if summary["blocked"] == summary["attempted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
