"""成本口径：缓存与非缓存必须分开，缺价不能当成免费。

这两条都是"错了也不会报错"的账：把缓存按全价算只是高估，
把缺价的角色按 0 累加则是**低报**——后者会直接误导选型。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analyze_eval import RoleUsage, _cached_tokens, role_cost
from retail_support_agent.evaluation import (
    FROZEN_NL_JUDGE,
    FROZEN_USER_SIMULATOR,
    PREVIOUS_USER_SIMULATOR,
)
from retail_support_agent.pricing import price_for


class TestCacheIsPricedSeparately:
    def test_cached_input_is_cheaper_than_uncached(self) -> None:
        price = price_for("deepseek-v4-pro")
        assert price is not None
        assert price.cached_input is not None
        full = price.cost(1_000_000, 0, cached_tokens=0)
        all_cached = price.cost(1_000_000, 0, cached_tokens=1_000_000)
        assert all_cached < full
        assert all_cached == pytest.approx(full * price.cached_input / price.input)

    def test_cache_never_exceeds_prompt(self) -> None:
        """服务端偶尔会报出比输入还多的命中数，不能因此把成本算成负的。"""
        price = price_for("deepseek-v4-pro")
        assert price is not None
        assert price.cached_input is not None
        assert price.cost(100, 0, cached_tokens=10_000) == pytest.approx(
            100 / 1e6 * price.cached_input
        )


class TestUnreportedCacheIsNotZero:
    """qwen3.6-plus 实测返回 `cached_tokens: null`——服务端没报，不是没命中。

    混作一谈会让报告里的「命中率 0%」被读成「这个模型没有缓存收益」。
    """

    def test_null_nested_detail_reads_as_unknown(self) -> None:
        usage = {"prompt_tokens": 397, "prompt_tokens_details": {"cached_tokens": None}}
        assert _cached_tokens(usage) is None

    def test_deepseek_flat_field_is_read(self) -> None:
        assert _cached_tokens({"prompt_cache_hit_tokens": 4864}) == 4864

    def test_reported_zero_is_a_real_zero(self) -> None:
        assert _cached_tokens({"prompt_tokens_details": {"cached_tokens": 0}}) == 0

    def test_unknown_cache_is_counted_separately_from_zero(self) -> None:
        unknown = RoleUsage(calls=3, prompt=1000, cache_unreported=3)
        real_zero = RoleUsage(calls=3, prompt=1000, cache_unreported=0)
        assert unknown.cache_is_unknown is True
        assert real_zero.cache_is_unknown is False


class TestMissingPriceIsNotFree:
    def test_unknown_model_returns_none_not_zero(self) -> None:
        """返回 0 会被无声地累加进合计，返回 None 才逼调用方处理。"""
        assert role_cost("some-model-we-never-priced", RoleUsage(prompt=10**6)) is None

    def test_every_frozen_stack_role_is_priced(self) -> None:
        """评测栈三个角色都必须能折算，否则总成本永远只是个下界。

        模拟用户此前不在快照里，实测它占三角色总成本的 40%——
        「无法折算」在报告里的效果等同于按 0 记。
        """
        # 旧模拟用户也必须留价：复跑换型前的历史结论时要按它折算成本，
        # 缺价会被静默当成 0，而它当年占三角色总成本的 40%。
        for model in (
            FROZEN_USER_SIMULATOR,
            PREVIOUS_USER_SIMULATOR,
            FROZEN_NL_JUDGE,
            "deepseek-v4-pro",
        ):
            assert price_for(model) is not None, f"{model} 缺价"


class TestReasoningIsInsideCompletion:
    """推理 token 已包含在 completion_tokens 里，不能再单独加一次。

    2026-09-06 实测 qwen3.6-plus：prompt 32 + completion 2865 = total 2897，
    reasoning 2709，2865 − 2709 = 156 正好是可见回复。
    再加一遍会把模拟用户的成本高估约 1.9 倍——而它占三角色总成本的 40%。
    """

    def test_cost_uses_completion_tokens_only(self) -> None:
        # 用 PREVIOUS_USER_SIMULATOR 而不是当前冻结型号：上面记录的那次实测
        # 就是在 qwen3.6-plus 上做的，换了模型这些数字就不再对应任何观测。
        price = price_for(PREVIOUS_USER_SIMULATOR)
        assert price is not None
        # 同一次调用：completion 2865（其中推理 2709）
        billed = price.cost(32, 2865)
        naive_double_count = price.cost(32, 2865 + 2709)
        assert billed < naive_double_count
        assert billed == pytest.approx(32 / 1e6 * 2.0 + 2865 / 1e6 * 12.0)

    def test_role_usage_does_not_add_reasoning_to_completion(self) -> None:
        """RoleUsage.reasoning 只用于报告占比，绝不进成本公式。"""
        usage = RoleUsage(calls=1, prompt=32, completion=2865, reasoning=2709)
        price = price_for(PREVIOUS_USER_SIMULATOR)
        assert price is not None
        assert role_cost(PREVIOUS_USER_SIMULATOR, usage) == pytest.approx(price.cost(32, 2865, 0))
