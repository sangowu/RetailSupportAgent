"""Tests for price conversion.

These assertions protect three costs that measurement exposed and that had been missed:
cache hits, reasoning tokens and judge calls. A changed number means either the price
snapshot needs updating or the conversion logic is wrong, and both need a human to look.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from retail_support_agent.pricing import (
    DASHSCOPE_IMPLICIT_CACHE_RATIO,
    DASHSCOPE_PRICES,
    Price,
    Tier,
    cache_ratio_is_known,
    deepseek_tier,
    price_for,
)

# Beijing time = UTC+8. Monday is 2026-09-07, Saturday is 2026-09-05.
MONDAY_PEAK = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)  # 10:00 Beijing, inside the 9-12 peak
MONDAY_OFF_PEAK = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)  # 13:00 Beijing, the lunch gap
MONDAY_SECOND_PEAK = datetime(2026, 9, 7, 7, 0, tzinfo=UTC)  # 15:00 Beijing, inside the 14-18 peak
SATURDAY = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)  # Off-peak all weekend


class TestCacheDiscount:
    def test_cached_tokens_cost_less_than_uncached(self) -> None:
        price = Price(input=10.0, output=20.0, cached_input=2.0)
        full = price.cost(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=0)
        cached = price.cost(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=1_000_000)
        assert full == pytest.approx(10.0)
        assert cached == pytest.approx(2.0)

    def test_partial_cache_hit_is_split_correctly(self) -> None:
        price = Price(input=10.0, output=20.0, cached_input=2.0)
        cost = price.cost(prompt_tokens=1_000_000, completion_tokens=0, cached_tokens=250_000)
        assert cost == pytest.approx(0.75 * 10.0 + 0.25 * 2.0)

    def test_cached_tokens_cannot_exceed_prompt_tokens(self) -> None:
        """Dirty data must not turn the cost negative."""
        price = Price(input=10.0, output=20.0, cached_input=2.0)
        cost = price.cost(prompt_tokens=1000, completion_tokens=0, cached_tokens=999_999)
        assert cost > 0

    def test_reference_model_uses_the_documented_20_percent(self) -> None:
        price = DASHSCOPE_PRICES["qwen3.7-max-2026-06-08"]
        assert price.cached_input == pytest.approx(price.input * DASHSCOPE_IMPLICIT_CACHE_RATIO)


class TestCachePriceIsLookedUpNotGuessed:
    """Three models were once marked "cache discount not published", which made the cost
    interval three times as wide. On 2026-09-06 explicit prices were found and the interval
    collapsed to a single value."""

    @pytest.mark.parametrize(
        ("model", "cached"),
        [("qwen3.8-max", 1.5), ("qwen3.8-flash", 0.1), ("deepseek-v4-pro-0813", 1.2)],
    )
    def test_looked_up_prices_beat_the_ratio_estimate(self, model: str, cached: float) -> None:
        price = DASHSCOPE_PRICES[model]
        assert price.cached_input is not None
        assert price.cached_input == pytest.approx(cached)
        assert cache_ratio_is_known(model) is True
        # Every looked-up price is cheaper than the 20% estimate, so the earlier ratio
        # estimate was conservative and pointed the right way
        assert price.cached_input < price.input * DASHSCOPE_IMPLICIT_CACHE_RATIO

    def test_models_without_a_looked_up_price_fall_back_to_the_ratio(self) -> None:
        price = DASHSCOPE_PRICES["glm-5.2"]
        assert price.cached_input == pytest.approx(price.input * DASHSCOPE_IMPLICIT_CACHE_RATIO)


class TestUnknownCacheRatioIsNotGuessed:
    """The mechanism stays: for a model whose price cannot be found, report an interval
    rather than pretend precision.

    The rule is tested with a synthetic price rather than tied to a specific model. Tying it
    to a model would break the test whenever a price changes, and this discipline must not
    depend on prices.
    """

    UNKNOWN = Price(input=10.0, output=20.0, cached_input=None)

    def test_unknown_ratio_bills_at_full_price(self) -> None:
        """Overestimate rather than underestimate: no known discount means no discount."""
        with_cache = self.UNKNOWN.cost(1_000_000, 0, cached_tokens=1_000_000)
        without_cache = self.UNKNOWN.cost(1_000_000, 0, cached_tokens=0)
        assert with_cache == pytest.approx(without_cache)

    def test_floor_gives_a_lower_bound_for_the_interval(self) -> None:
        high = self.UNKNOWN.cost(1_000_000, 0, cached_tokens=1_000_000)
        low = self.UNKNOWN.cost_floor(1_000_000, 0, cached_tokens=1_000_000)
        assert low < high
        assert low == pytest.approx(self.UNKNOWN.input * DASHSCOPE_IMPLICIT_CACHE_RATIO)

    def test_reference_model_needs_no_interval(self) -> None:
        assert cache_ratio_is_known("qwen3.7-max-2026-06-08") is True


class TestDeepSeekPeakHours:
    """The judge runs on DeepSeek, where peak hours cost twice as much, so the same
    re-judgement costs a different amount depending on when it runs."""

    @pytest.mark.parametrize("moment", [MONDAY_PEAK, MONDAY_SECOND_PEAK])
    def test_beijing_business_hours_are_peak(self, moment: datetime) -> None:
        assert deepseek_tier(moment) is Tier.PEAK

    def test_lunch_gap_is_off_peak(self) -> None:
        assert deepseek_tier(MONDAY_OFF_PEAK) is Tier.OFF_PEAK

    def test_weekend_is_off_peak_all_day(self) -> None:
        assert deepseek_tier(SATURDAY) is Tier.OFF_PEAK

    def test_off_peak_is_exactly_half_of_peak(self) -> None:
        peak = price_for("deepseek-v4-flash", MONDAY_PEAK)
        off = price_for("deepseek-v4-flash", MONDAY_OFF_PEAK)
        assert peak is not None and off is not None
        assert off.input == pytest.approx(peak.input / 2)
        assert off.output == pytest.approx(peak.output / 2)
        assert off.cached_input is not None and peak.cached_input is not None
        assert off.cached_input == pytest.approx(peak.cached_input / 2)

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        assert deepseek_tier(MONDAY_PEAK.replace(tzinfo=None)) is Tier.PEAK


class TestJudgeIsCheapButNotFree:
    """The judge cost was previously reported as unknown, and unknown is not zero."""

    def test_flash_judge_cost_is_small_but_nonzero(self) -> None:
        price = price_for("deepseek-v4-flash", MONDAY_PEAK)
        assert price is not None
        # A typical judgement: about 6k input and 300 output tokens
        cost = price.cost(prompt_tokens=6000, completion_tokens=300, cached_tokens=0)
        assert 0 < cost < 0.05

    def test_deepseek_pro_judge_would_cost_three_times_more(self) -> None:
        flash = price_for("deepseek-v4-flash", MONDAY_PEAK)
        pro = price_for("deepseek-v4-pro", MONDAY_PEAK)
        assert flash is not None and pro is not None
        assert pro.input == pytest.approx(flash.input * 3)
        assert pro.output == pytest.approx(flash.output * 3)


def test_dashscope_and_deepseek_names_do_not_collide() -> None:
    """deepseek-v4-pro-0813 on DashScope and DeepSeek's own deepseek-v4-pro do not share a
    price list, so the names must stay distinct or costs are converted from the wrong table."""
    assert "deepseek-v4-pro" not in DASHSCOPE_PRICES
    assert price_for("deepseek-v4-pro-0813") is DASHSCOPE_PRICES["deepseek-v4-pro-0813"]
    assert price_for("deepseek-v4-pro") is not DASHSCOPE_PRICES["deepseek-v4-pro-0813"]


def test_unknown_model_returns_no_price_instead_of_guessing() -> None:
    assert price_for("some-model-we-never-priced") is None
