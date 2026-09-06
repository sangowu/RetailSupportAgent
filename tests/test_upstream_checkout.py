"""对照真实检出的校验；未拉取上游时跳过，不伪装成通过。"""

from __future__ import annotations

import subprocess

import pytest

from retail_support_agent.tau2_pin import DEFAULT_CHECKOUT, verify_checkout

pytestmark = pytest.mark.skipif(
    not (DEFAULT_CHECKOUT / ".git").is_dir(),
    reason="未拉取 τ²-bench，先运行 scripts/fetch_tau2.py",
)


def test_checkout_matches_manifest() -> None:
    mismatches = verify_checkout()
    assert not mismatches, "\n".join(m.describe() for m in mismatches)


def test_checkout_disables_line_ending_conversion() -> None:
    """Windows 的 autocrlf 会改写上游文件，使同一 commit 在不同平台算出不同摘要。"""
    result = subprocess.run(
        ["git", "config", "--get", "core.autocrlf"],
        cwd=DEFAULT_CHECKOUT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "false", (
        "上游检出必须以 core.autocrlf=false 配置，否则 SHA256 固定不跨平台成立；"
        "重新运行 scripts/fetch_tau2.py 修复"
    )
