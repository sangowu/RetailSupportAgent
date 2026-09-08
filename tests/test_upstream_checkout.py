"""Verification against the real checkout. Skipped when upstream has not been
fetched, rather than passing vacuously."""

from __future__ import annotations

import subprocess

import pytest

from retail_support_agent.tau2_pin import DEFAULT_CHECKOUT, verify_checkout

pytestmark = pytest.mark.skipif(
    not (DEFAULT_CHECKOUT / ".git").is_dir(),
    reason="τ²-bench not fetched; run scripts/fetch_tau2.py first",
)


def test_checkout_matches_manifest() -> None:
    mismatches = verify_checkout()
    assert not mismatches, "\n".join(m.describe() for m in mismatches)


def test_checkout_disables_line_ending_conversion() -> None:
    """Windows autocrlf rewrites upstream files, so the same commit would hash
    differently per platform."""
    result = subprocess.run(
        ["git", "config", "--get", "core.autocrlf"],
        cwd=DEFAULT_CHECKOUT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "false", (
        "the upstream checkout must set core.autocrlf=false, or the SHA256 pin "
        "does not hold across platforms. Re-run scripts/fetch_tau2.py to fix it"
    )
