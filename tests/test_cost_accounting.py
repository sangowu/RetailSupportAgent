"""Cost conventions: cached and uncached input are priced separately, and a missing price
is not free.

Both are accounting errors that raise nothing. Charging cached input at full price only
overestimates; adding a role with no price as 0 underreports, and that misleads model
selection directly.
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
        """The server sometimes reports more cache hits than input tokens, which must not
        turn the cost negative."""
        price = price_for("deepseek-v4-pro")
        assert price is not None
        assert price.cached_input is not None
        assert price.cost(100, 0, cached_tokens=10_000) == pytest.approx(
            100 / 1e6 * price.cached_input
        )


class TestUnreportedCacheIsNotZero:
    """qwen3.6-plus was measured returning `cached_tokens: null`: the server did not report
    a value, which is not the same as no cache hit.

    Conflating the two makes a reported "0% hit rate" read as "this model gains nothing from
    caching".
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
        """Returning 0 is silently added to the total; returning None forces the caller to
        handle it."""
        assert role_cost("some-model-we-never-priced", RoleUsage(prompt=10**6)) is None

    def test_every_frozen_stack_role_is_priced(self) -> None:
        """All three roles of the evaluation stack must be convertible, or the total cost is
        only ever a lower bound.

        The simulated user was previously absent from the snapshot and was measured at 40% of
        the three-role total. In a report, "cannot be converted" has the same effect as
        counting it as 0.
        """
        # The previous simulated user keeps its price too: reproducing conclusions from
        # before the switch converts costs with it, a missing price is silently treated as 0,
        # and it accounted for 40% of the three-role total at the time.
        for model in (
            FROZEN_USER_SIMULATOR,
            PREVIOUS_USER_SIMULATOR,
            FROZEN_NL_JUDGE,
            "deepseek-v4-pro",
        ):
            assert price_for(model) is not None, f"{model} has no price"


class TestReasoningIsInsideCompletion:
    """Reasoning tokens are already inside completion_tokens and must not be added again.

    Measured on qwen3.6-plus on 2026-09-06: prompt 32 + completion 2865 = total 2897, with
    reasoning 2709, and 2865 - 2709 = 156 is exactly the visible reply.
    Adding them again overestimates the simulated user's cost by about 1.9x, and that role is
    40% of the three-role total.
    """

    def test_cost_uses_completion_tokens_only(self) -> None:
        # PREVIOUS_USER_SIMULATOR rather than the currently frozen model: the measurement
        # recorded above was made on qwen3.6-plus, and with another model these numbers no
        # longer correspond to any observation.
        price = price_for(PREVIOUS_USER_SIMULATOR)
        assert price is not None
        # The same call: completion 2865, of which 2709 is reasoning
        billed = price.cost(32, 2865)
        naive_double_count = price.cost(32, 2865 + 2709)
        assert billed < naive_double_count
        assert billed == pytest.approx(32 / 1e6 * 2.0 + 2865 / 1e6 * 12.0)

    def test_role_usage_does_not_add_reasoning_to_completion(self) -> None:
        """RoleUsage.reasoning is only for reporting a share and never enters the cost
        formula."""
        usage = RoleUsage(calls=1, prompt=32, completion=2865, reasoning=2709)
        price = price_for(PREVIOUS_USER_SIMULATOR)
        assert price is not None
        assert role_cost(PREVIOUS_USER_SIMULATOR, usage) == pytest.approx(price.cost(32, 2865, 0))
