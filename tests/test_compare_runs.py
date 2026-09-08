"""Numerical correctness of the McNemar exact test.

A wrong statistic raises nothing. It produces a plausible-looking p-value
that then goes into a report as a conclusion, so the closed-form values are
pinned here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from compare_runs import mcnemar_exact


class TestMcNemarExact:
    def test_no_disagreement_gives_no_evidence(self) -> None:
        """No task changed means no evidence, which is not the same as "they are
        equal"."""
        assert mcnemar_exact(0, 0) == 1.0

    def test_reproduces_the_thinking_ab_result(self) -> None:
        """The 2026-09-06 simulated-user thinking on/off comparison: 6 fixed,
        1 broken."""
        assert mcnemar_exact(1, 6) == pytest.approx(0.125)

    def test_reproduces_the_model_selection_result(self) -> None:
        """qwen3.8-max vs deepseek-v4-pro paired on the holdout: 8 to 0."""
        assert mcnemar_exact(0, 8) == pytest.approx(2 / 256)

    def test_is_symmetric(self) -> None:
        """Which model is the baseline must not change the p-value."""
        assert mcnemar_exact(3, 9) == mcnemar_exact(9, 3)

    def test_all_one_sided_is_the_binomial_tail(self) -> None:
        """With b=0 it reduces to 2 * (1/2)^n."""
        for n in (1, 4, 10):
            assert mcnemar_exact(0, n) == pytest.approx(2 * 0.5**n)

    def test_probability_never_exceeds_one(self) -> None:
        """Symmetric disagreement makes the two-sided sum exceed 1, so it is clamped."""
        assert mcnemar_exact(5, 5) == 1.0
        assert mcnemar_exact(1, 1) == 1.0
