"""价格折算的测试。

这些断言保护三笔实测发现、此前一直漏算的账：缓存命中、推理 token、判官调用。
数字变了要么是价格快照该更新，要么是折算逻辑写错了——两种都必须有人看一眼。
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

# 北京时间 = UTC+8。周一 2026-09-07，周六 2026-09-05。
MONDAY_PEAK = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)  # 北京 10:00，落在 9-12 高峰
MONDAY_OFF_PEAK = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)  # 北京 13:00，午休空档
MONDAY_SECOND_PEAK = datetime(2026, 9, 7, 7, 0, tzinfo=UTC)  # 北京 15:00，落在 14-18 高峰
SATURDAY = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)  # 周末全天空闲


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
        """脏数据不该让成本变成负数。"""
        price = Price(input=10.0, output=20.0, cached_input=2.0)
        cost = price.cost(prompt_tokens=1000, completion_tokens=0, cached_tokens=999_999)
        assert cost > 0

    def test_reference_model_uses_the_documented_20_percent(self) -> None:
        price = DASHSCOPE_PRICES["qwen3.7-max-2026-06-08"]
        assert price.cached_input == pytest.approx(price.input * DASHSCOPE_IMPLICIT_CACHE_RATIO)


class TestCachePriceIsLookedUpNotGuessed:
    """三个模型曾被标为"缓存折扣未公开"，导致成本区间三倍宽；
    2026-09-06 查到了明确单价，区间收敛为确定值。"""

    @pytest.mark.parametrize(
        ("model", "cached"),
        [("qwen3.8-max", 1.5), ("qwen3.8-flash", 0.1), ("deepseek-v4-pro-0813", 1.2)],
    )
    def test_looked_up_prices_beat_the_ratio_estimate(self, model: str, cached: float) -> None:
        price = DASHSCOPE_PRICES[model]
        assert price.cached_input is not None
        assert price.cached_input == pytest.approx(cached)
        assert cache_ratio_is_known(model) is True
        # 查到的实价都比按 20% 推算的便宜——说明当初按比例估是保守的，方向没错
        assert price.cached_input < price.input * DASHSCOPE_IMPLICIT_CACHE_RATIO

    def test_models_without_a_looked_up_price_fall_back_to_the_ratio(self) -> None:
        price = DASHSCOPE_PRICES["glm-5.2"]
        assert price.cached_input == pytest.approx(price.input * DASHSCOPE_IMPLICIT_CACHE_RATIO)


class TestUnknownCacheRatioIsNotGuessed:
    """机制仍要保留：真遇到查不到价的模型，必须给区间而不是假装精确。

    用合成价格测这条规则，而不是绑在某个具体模型上——那样价格一变测试就失效，
    而这条纪律是不该随价格变动的。
    """

    UNKNOWN = Price(input=10.0, output=20.0, cached_input=None)

    def test_unknown_ratio_bills_at_full_price(self) -> None:
        """宁可高估，不可低估——没拿到折扣就当没有折扣。"""
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
    """判官在 DeepSeek 上，高峰时段贵一倍——同一次复判的花费会随时间变。"""

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
    """判官此前报告为"未知"，未知不等于零。"""

    def test_flash_judge_cost_is_small_but_nonzero(self) -> None:
        price = price_for("deepseek-v4-flash", MONDAY_PEAK)
        assert price is not None
        # 一次典型判定：约 6k 输入、300 输出
        cost = price.cost(prompt_tokens=6000, completion_tokens=300, cached_tokens=0)
        assert 0 < cost < 0.05

    def test_deepseek_pro_judge_would_cost_three_times_more(self) -> None:
        flash = price_for("deepseek-v4-flash", MONDAY_PEAK)
        pro = price_for("deepseek-v4-pro", MONDAY_PEAK)
        assert flash is not None and pro is not None
        assert pro.input == pytest.approx(flash.input * 3)
        assert pro.output == pytest.approx(flash.output * 3)


def test_dashscope_and_deepseek_names_do_not_collide() -> None:
    """DashScope 上的 deepseek-v4-pro-0813 与 DeepSeek 官方的 deepseek-v4-pro
    不是一套价格，名字也必须区分开，否则会按错的价目表折算。"""
    assert "deepseek-v4-pro" not in DASHSCOPE_PRICES
    assert price_for("deepseek-v4-pro-0813") is DASHSCOPE_PRICES["deepseek-v4-pro-0813"]
    assert price_for("deepseek-v4-pro") is not DASHSCOPE_PRICES["deepseek-v4-pro-0813"]


def test_unknown_model_returns_no_price_instead_of_guessing() -> None:
    assert price_for("some-model-we-never-priced") is None
