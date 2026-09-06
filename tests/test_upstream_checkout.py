"""对照真实检出的校验；未拉取上游时跳过，不伪装成通过。"""

from __future__ import annotations

import pytest

from retail_support_agent.tau2_pin import DEFAULT_CHECKOUT, verify_checkout

pytestmark = pytest.mark.skipif(
    not (DEFAULT_CHECKOUT / ".git").is_dir(),
    reason="未拉取 τ²-bench，先运行 scripts/fetch_tau2.py",
)


def test_checkout_matches_manifest() -> None:
    mismatches = verify_checkout()
    assert not mismatches, "\n".join(m.describe() for m in mismatches)
