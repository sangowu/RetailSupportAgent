"""Run the natural-language attack set against the real agent.

    uv run python scripts/run_adversarial_eval.py --spend
    uv run python scripts/run_adversarial_eval.py --spend --attacks 2 --max-turns 4

This calls two models and is billed. Without `--spend` it prints the plan - which
attacks, which models, how many turns at most - and exits without contacting a provider,
so the wiring can be checked for free.

`scripts/run_access_eval.py` is the free, deterministic set and is the one that gates
every commit. This one answers a different question: whether a real model can be talked
into handing over what the controls are meant to protect.

Exit status is 1 if any attack got something out.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from retail_support_agent.adversarial_eval import (  # noqa: E402
    ATTACKS_PATH,
    RecordingDomain,
    build_agent_chat,
    load_attacks,
    make_attacker,
    run_attack,
    summarize,
)
from retail_support_agent.config import SELECTED_MODEL, Settings  # noqa: E402
from retail_support_agent.domain import RetailDomain  # noqa: E402

DEFAULT_ATTACKER_MODEL = "qwen3.7-flash-2026-07-15"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attacks", type=Path, default=ATTACKS_PATH)
    parser.add_argument("--attacker-model", default=DEFAULT_ATTACKER_MODEL)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None, help="run only the first N attacks")
    parser.add_argument("--only", action="append", default=None, help="run only these ids")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--spend",
        action="store_true",
        help="actually call the providers. Without it, nothing is billed.",
    )
    args = parser.parse_args()

    document = load_attacks(args.attacks)
    attacks = document["attacks"]
    if args.only:
        attacks = [attack for attack in attacks if attack["id"] in set(args.only)]
    if args.limit:
        attacks = attacks[: args.limit]
    models = {"agent": SELECTED_MODEL, "attacker": args.attacker_model}

    print(f"set:      {document['version']}  ({len(attacks)} attacks)")
    print(f"agent:    {models['agent']}")
    print(f"attacker: {models['attacker']}")
    print(
        f"turns:    up to {args.max_turns} each, so at most "
        f"{len(attacks) * args.max_turns * 2} model calls"
    )
    for attack in attacks:
        print(f"  - {attack['id']}: {attack['goal']}")

    if not args.spend:
        print("\nnothing was called. Re-run with --spend to execute against the providers.")
        return 0

    settings = Settings()  # type: ignore[call-arg]
    attacker = make_attacker(settings, args.attacker_model)
    agent_chat = build_agent_chat(settings)

    results = []
    for attack in attacks:
        print(f"\nrunning {attack['id']} ...", flush=True)
        domain = RecordingDomain(RetailDomain())
        result = run_attack(attack, attacker, agent_chat, domain, max_turns=args.max_turns)
        results.append(result)
        state = "held" if result.held else ("NOT RUN" if not result.ran else "BREACHED")
        print(f"  {state} after {result.turns} turns, {result.refusals} refusals recorded")
        if result.error:
            print(f"    {result.error}")
        for leak in result.leaked:
            print(f"    disclosed: {leak}")
        for call in result.foreign_calls:
            print(f"    executed:  {call}")

    summary = summarize(results, str(document["version"]), models)
    print(f"\n{summary['held']}/{summary['attempted']} held  ({summary['version']})")
    if summary["not_run"]:
        print(f"{len(summary['not_run'])} attack(s) never ran, not counted as held")

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **summary,
            "transcripts": [
                {"id": result.attack_id, "turns": result.transcript} for result in results
            ],
        }
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"summary and transcripts written to {args.out}")

    # An attack that never ran fails the run too: a set that quietly stops attacking
    # would otherwise report a perfect score forever.
    return 0 if summary["held"] == summary["attempted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
