"""M0.2: run capability probes against each candidate model and record the raw results.

This makes real API calls and costs money, though the prompts are tiny and the
output caps low, so the amount is negligible.

Usage:
    uv run python scripts/probe_models.py                    # every candidate
    uv run python scripts/probe_models.py --models glm-5.2   # one model
    uv run python scripts/probe_models.py --dry-run          # list the probes only

Results go to evaluation/capability_checks/<UTC timestamp>/ with per-model raw
records and a summary. No key appears in any output.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openai import OpenAI

from retail_support_agent import model_probe as mp
from retail_support_agent.config import CANDIDATE_MODELS, Settings
from retail_support_agent.tau2_pin import REPO_ROOT

OUTPUT_ROOT = REPO_ROOT / "evaluation" / "capability_checks"

# The levels probed are the union of every provider's documentation: which ones
# are accepted is the result being measured, not an assumption to start from.
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


def probe_plan() -> list[tuple[str, Callable[[OpenAI, str], mp.ProbeOutcome]]]:
    """Probe order. basic comes first: if it fails there is no point spending more."""
    plan: list[tuple[str, Callable[[OpenAI, str], mp.ProbeOutcome]]] = [
        ("basic", mp.probe_basic),
        ("tool_call", mp.probe_tool_call),
        ("tool_result_roundtrip", mp.probe_tool_result_roundtrip),
        ("stream_usage", mp.probe_stream_usage),
        ("sampling_params", mp.probe_sampling_params),
        ("truncation", mp.probe_truncation),
        ("thinking_off", mp.probe_thinking_off),
    ]
    # First read each level's mapped budget out of the server error under a small
    # output cap, then measure what actually takes effect with no cap.
    plan += [
        (
            f"effort_budget_mapping_{level}",
            (lambda lv: lambda c, m: mp.probe_effort_budget_mapping(c, m, lv))(level),
        )
        for level in EFFORT_LEVELS
    ]
    plan += [
        (
            f"reasoning_effort_{level}",
            (lambda lv: lambda c, m: mp.probe_reasoning_effort(c, m, lv))(level),
        )
        for level in EFFORT_LEVELS
    ]
    plan += [
        ("thinking_budget_64", lambda c, m: mp.probe_thinking_budget(c, m, 64)),
        ("thinking_mutual_exclusion", mp.probe_mutual_exclusion),
    ]
    return plan


@dataclass
class ModelReport:
    """All probe results for one model."""

    model_id_requested: str
    outcomes: list[mp.ProbeOutcome]

    def to_json(self) -> dict[str, Any]:
        return {
            "model_id_requested": self.model_id_requested,
            "probe_count": len(self.outcomes),
            "outcomes": [o.to_json() for o in self.outcomes],
        }


def run_model(client: OpenAI, model: str) -> ModelReport:
    """Run every probe for one model. An unavailable model is recorded as such and
    the run stops; no substitute model is used."""
    plan = probe_plan()
    outcomes: list[mp.ProbeOutcome] = []

    print(f"\n=== {model} ===", flush=True)
    for name, probe in plan:
        outcome = probe(client, model)
        outcomes.append(outcome)
        detail = outcome.findings or (outcome.error or {})
        print(f"  {name:<28} {outcome.status:<9} {json.dumps(detail, ensure_ascii=False)[:110]}")

        if name == "basic" and outcome.status != "ok":
            print("  basic failed; skipping this model's remaining probes")
            break

    return ModelReport(model, outcomes)


def main() -> int:
    parser = argparse.ArgumentParser(description="M0.2 model capability probes (spends real money)")
    parser.add_argument("--models", nargs="*", default=list(CANDIDATE_MODELS))
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan without sending requests"
    )
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    if args.dry_run:
        names = [name for name, _ in probe_plan()]
        total = len(args.models) * len(names)
        print(f"{len(args.models)} models x {len(names)} probes = {total} requests")
        print("models: " + ", ".join(args.models))
        print("probes: " + ", ".join(names))
        return 0

    try:
        settings = Settings()  # type: ignore[call-arg]  # fields come from env/.env
    except Exception as exc:
        print(
            f"failed to read configuration (check DASHSCOPE_API_KEY in .env): {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    client = mp.build_client(settings)
    run_dir = OUTPUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    reports = [run_model(client, model) for model in args.models]

    for report in reports:
        name = report.model_id_requested.replace("/", "_")
        (run_dir / f"{name}.json").write_text(
            json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    summary = {
        "run_at": datetime.now(UTC).isoformat(),
        "base_url": settings.base_url,
        "api_key": settings.key_presence(),  # presence only, never any key content
        "models": args.models,
        "note": (
            "HTTP 200 does not mean a parameter took effect; a conclusion needs a "
            "difference in reasoning_tokens or an explicit server error as evidence."
        ),
        "per_model_status": {
            r.model_id_requested: {o.name: o.status for o in r.outcomes} for r in reports
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    print(f"\nraw records written to {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
