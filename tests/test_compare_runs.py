"""McNemar 精确检验的数值正确性。

统计量算错不会报错，只会给出一个看起来很像那么回事的 p 值，
然后被写进报告当结论。所以用手算得出的封闭解钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from compare_runs import mcnemar_exact


class TestMcNemarExact:
    def test_no_disagreement_gives_no_evidence(self) -> None:
        """一题都没变，就是没有任何证据——不是「两者相同」。"""
        assert mcnemar_exact(0, 0) == 1.0

    def test_reproduces_the_thinking_ab_result(self) -> None:
        """2026-09-06 模拟用户开/关思考的对照：修好 6 题、弄坏 1 题。"""
        assert mcnemar_exact(1, 6) == pytest.approx(0.125)

    def test_reproduces_the_model_selection_result(self) -> None:
        """qwen3.8-max vs deepseek-v4-pro 保留集配对：8 比 0。"""
        assert mcnemar_exact(0, 8) == pytest.approx(2 / 256)

    def test_is_symmetric(self) -> None:
        """哪个模型当基线不该改变 p 值。"""
        assert mcnemar_exact(3, 9) == mcnemar_exact(9, 3)

    def test_all_one_sided_is_the_binomial_tail(self) -> None:
        """b=0 时退化成 2 × (1/2)^n。"""
        for n in (1, 4, 10):
            assert mcnemar_exact(0, n) == pytest.approx(2 * 0.5**n)

    def test_probability_never_exceeds_one(self) -> None:
        """对称的不一致会让双尾超过 1，必须截断。"""
        assert mcnemar_exact(5, 5) == 1.0
        assert mcnemar_exact(1, 1) == 1.0
