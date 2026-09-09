"""The measurement M5 optimizes against: cost priced by the run's clock, and latency.

The defect these tests pin down was silent. `price_for` defaults its moment to now,
DeepSeek's peak rate is exactly twice its off-peak rate, and the analysis script printed
whichever of the two the reader happened to be sitting in. It only surfaced because the
cross-check against upstream's own accounting disagreed by a factor of two, and a factor
of two in the cost of a run is the difference between accepting and rejecting a
configuration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from retail_support_agent.pricing import Tier, deepseek_tier, price_for
from retail_support_agent.run_metrics import (
    Distribution,
    ModelCall,
    RunClock,
    cached_tokens_of,
    latency_profile,
    model_calls,
    passed_count,
    per_successful_task,
    percentile,
    price_varies_with_time,
    recover_clock,
    reference_moment,
    rewards_by_task,
    run_cost,
    run_started_at,
)

REPO = Path(__file__).resolve().parents[1]

DEEPSEEK = "deepseek-v4-pro"
FLAT = "qwen3.8-flash"  # DashScope, one rate at every hour of the day

RUN_START = datetime(2026, 9, 8, 12, 18, 0, tzinfo=UTC)  # Beijing 20:18, off peak
OFFSET = timedelta(hours=1)  # the machine that produced the runs on disk


def message(
    role: str = "assistant",
    *,
    at: datetime | None = None,
    prompt: int = 1000,
    completion: int = 100,
    cached: int | None = 800,
    reasoning: int = 0,
    seconds: float | None = 2.0,
) -> dict[str, Any]:
    """One message as tau2 records it: naive local timestamp, usage inside raw_data."""
    usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached is not None:
        usage["prompt_cache_hit_tokens"] = cached
    if reasoning:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    stamp = (at or RUN_START) + OFFSET
    return {
        "role": role,
        "timestamp": stamp.replace(tzinfo=None).isoformat(),
        "raw_data": {"usage": usage},
        "generation_time_seconds": seconds,
    }


def simulation(
    task_id: str = "0",
    *,
    messages: list[dict[str, Any]] | None = None,
    reward: float | None = 1.0,
    duration: float = 60.0,
) -> dict[str, Any]:
    sim: dict[str, Any] = {
        "task_id": task_id,
        "duration": duration,
        "messages": messages if messages is not None else [message()],
    }
    if reward is not None:
        sim["reward_info"] = {"reward": reward}
    return sim


def summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": "20260908T121800Z", "wall_seconds": 3600.0}
    base.update(overrides)
    return base


class TestTheRunsClock:
    def test_a_recorded_offset_is_used_as_recorded(self) -> None:
        clock = recover_clock(summary(local_utc_offset_seconds=7200), [])
        assert clock.source == "recorded"
        assert clock.offset == timedelta(hours=2)

    def test_the_offset_is_recovered_from_the_run_id(self) -> None:
        """The first message is the opening line of the first task, generated locally a
        moment after the run id is taken."""
        first = message(at=RUN_START + timedelta(seconds=1))
        clock = recover_clock(summary(), [simulation(messages=[first])])
        assert clock.source == "recovered"
        assert clock.offset == OFFSET

    def test_a_gap_that_is_not_a_timezone_is_refused(self) -> None:
        """Half an hour past the quarter hour is not an offset any machine has. Rounding
        it would invent an hour, and an invented hour changes the rate."""
        stray = message(at=RUN_START + timedelta(minutes=37))
        clock = recover_clock(summary(), [simulation(messages=[stray])])
        assert clock.offset is None
        assert "quarter hour" in clock.note

    def test_a_resumed_run_keeps_its_offset_and_says_so(self) -> None:
        """A resume writes into the same directory days later and never updates
        summary.json. That is not evidence the offset is wrong."""
        late = message(at=RUN_START + timedelta(hours=5))
        clock = recover_clock(summary(), [simulation(messages=[message(), late])])
        assert clock.offset == OFFSET
        assert "resumed" in clock.note

    def test_a_run_with_no_id_has_no_clock(self) -> None:
        clock = recover_clock({"wall_seconds": 10.0}, [simulation()])
        assert clock.offset is None
        assert clock.source == "unavailable"

    def test_the_run_id_is_the_start_and_run_at_is_not(self) -> None:
        """`run_at` is written when the run finishes. Using it as the start would place
        every call after the run was over."""
        assert run_started_at(summary()) == RUN_START
        assert run_started_at({"run_id": "not a stamp"}) is None

    def test_naive_timestamps_become_utc(self) -> None:
        clock = RunClock(OFFSET, "recorded")
        naive = (RUN_START + OFFSET).replace(tzinfo=None)
        assert clock.to_utc(naive) == RUN_START

    def test_without_an_offset_no_moment_is_produced(self) -> None:
        assert RunClock(None, "unavailable").to_utc(datetime(2026, 9, 8, 13, 18)) is None


class TestCostIsPricedByTheRunsClock:
    def test_the_same_run_costs_the_same_whenever_it_is_analyzed(self) -> None:
        """The defect: the cost of a finished run used to depend on when it was read.

        Both moments below are real analysis times in the two tiers. The call's own
        moment is what decides the rate, so neither changes the answer.
        """
        call = ModelCall("0", "assistant", RUN_START, 1000, 100, 800, True, 0, 2.0)
        at_run_time = run_cost(DEEPSEEK, [call])
        assert at_run_time.total == run_cost(DEEPSEEK, [call], fallback=None).total
        priced = price_for(DEEPSEEK, RUN_START)
        assert priced is not None
        assert at_run_time.total == pytest.approx(priced.cost(1000, 100, 800))

    def test_peak_costs_exactly_twice_off_peak(self) -> None:
        """The factor that made the old figure wrong, stated as a fact of the price
        snapshot rather than as an assumption."""
        call = ModelCall("0", "assistant", None, 1000, 100, 800, True, 0, None)
        peak = run_cost(DEEPSEEK, [call], tier_moment=reference_moment(Tier.PEAK))
        off = run_cost(DEEPSEEK, [call], tier_moment=reference_moment(Tier.OFF_PEAK))
        assert peak.total == pytest.approx(off.total * 2)

    def test_the_reference_moments_really_are_in_their_tiers(self) -> None:
        for tier in (Tier.PEAK, Tier.OFF_PEAK):
            assert deepseek_tier(reference_moment(tier)) is tier

    def test_a_run_crossing_a_boundary_is_priced_on_both_sides(self) -> None:
        """Peak is Beijing 09-12 and 14-18, so a run of a few hours can straddle a
        boundary. Charging all of it at one tier is wrong on one side of it."""
        before = ModelCall(
            "0", "assistant", reference_moment(Tier.OFF_PEAK), 1000, 100, 0, True, 0, None
        )
        during = ModelCall(
            "1", "assistant", reference_moment(Tier.PEAK), 1000, 100, 0, True, 0, None
        )
        cost = run_cost(DEEPSEEK, [before, during])
        assert set(cost.tiers_used) == {Tier.PEAK, Tier.OFF_PEAK}
        assert cost.by_tier[Tier.PEAK] == 1
        one_sided = run_cost(DEEPSEEK, [before, during], tier_moment=reference_moment(Tier.PEAK))
        assert cost.total < one_sided.total

    def test_a_call_with_no_moment_is_unpriced_not_priced_at_now(self) -> None:
        """Pricing an undated call at the current time is the original defect. It is
        reported as unpriced instead, and unpriced is not free."""
        call = ModelCall("0", "assistant", None, 1000, 100, 0, True, 0, None)
        cost = run_cost(DEEPSEEK, [call])
        assert not cost.known
        assert cost.total == 0.0
        assert cost.unpriced_calls == 1

    def test_a_flat_rate_model_needs_no_moment(self) -> None:
        call = ModelCall("0", "assistant", None, 1000, 100, 0, True, 0, None)
        assert not price_varies_with_time(FLAT)
        assert run_cost(FLAT, [call]).known

    def test_an_unknown_model_is_unpriced_rather_than_free(self) -> None:
        call = ModelCall("0", "assistant", RUN_START, 1000, 100, 0, True, 0, None)
        cost = run_cost("a-model-nobody-priced", [call])
        assert not cost.known
        assert cost.unpriced_calls == 1

    def test_the_fallback_prices_calls_whose_moment_is_unknown(self) -> None:
        call = ModelCall("0", "assistant", None, 1000, 100, 0, True, 0, None)
        cost = run_cost(DEEPSEEK, [call], fallback=RUN_START)
        assert cost.known
        assert cost.undated_calls == 1


class TestCallsCarryTheirTask:
    def test_a_comparison_can_charge_both_runs_for_the_same_tasks(self) -> None:
        clock = RunClock(OFFSET, "recorded")
        sims = [simulation("3"), simulation("7")]
        calls = model_calls(sims, clock)
        assert sorted(call.task for call in calls) == ["3", "7"]

    def test_messages_without_usage_are_not_calls(self) -> None:
        """The opening line of every task is generated locally and reaches no model.
        Counting it as a call would report a latency of zero for it."""
        opening = {"role": "assistant", "timestamp": "2026-09-08T13:18:00"}
        calls = model_calls(
            [simulation(messages=[opening, message()])], RunClock(OFFSET, "recorded")
        )
        assert len(calls) == 1

    def test_only_the_requested_role_is_returned(self) -> None:
        sims = [simulation(messages=[message("assistant"), message("user")])]
        calls = model_calls(sims, RunClock(OFFSET, "recorded"), role="assistant")
        assert [call.role for call in calls] == ["assistant"]

    def test_a_missing_cache_field_is_unknown_rather_than_zero(self) -> None:
        sims = [simulation(messages=[message(cached=None)])]
        call = model_calls(sims, RunClock(OFFSET, "recorded"))[0]
        assert call.cache_reported is False
        assert cached_tokens_of({"prompt_tokens": 5}) is None


class TestLatency:
    def test_percentiles_are_observations_and_not_interpolations(self) -> None:
        """A p95 quoted as a measurement has to be a measurement. Interpolating between
        two calls reports a latency that nothing observed."""
        values = [float(n) for n in range(1, 21)]
        assert percentile(values, 95) == 19.0
        assert percentile(values, 50) == 10.0
        assert percentile(values, 100) == 20.0
        assert percentile([4.0], 95) == 4.0

    def test_a_percentile_of_nothing_is_an_error_not_a_zero(self) -> None:
        with pytest.raises(ValueError, match="no values"):
            percentile([], 95)

    def test_calls_without_a_generation_time_are_excluded(self) -> None:
        """Recording them as zero would pull the median towards a latency the system
        never had."""
        sims = [simulation(messages=[message(seconds=3.0), message(seconds=None)])]
        profile = latency_profile(model_calls(sims, RunClock(OFFSET, "recorded")), sims)
        assert profile.calls_without_timing == 1
        assert profile.per_call is not None
        assert profile.per_call.count == 1
        assert profile.per_call.p50 == 3.0

    def test_task_duration_is_kept_apart_from_call_latency(self) -> None:
        """Task duration contains the simulated user and the tools, and is inflated by
        concurrency. It is reported, and it is not latency."""
        sims = [simulation("0", duration=90.0), simulation("1", duration=30.0)]
        profile = latency_profile(model_calls(sims, RunClock(OFFSET, "recorded")), sims)
        assert profile.per_task is not None
        assert profile.per_task.total == 120.0
        assert profile.model_share == pytest.approx(4.0 / 120.0)

    def test_a_run_with_no_timings_reports_nothing_rather_than_zero(self) -> None:
        sims = [simulation(messages=[message(seconds=None)], duration=0.0)]
        profile = latency_profile(model_calls(sims, RunClock(OFFSET, "recorded")), sims)
        assert profile.per_call is None
        assert profile.per_task is None
        assert profile.model_share is None

    def test_a_distribution_of_nothing_is_none(self) -> None:
        assert Distribution.of([]) is None


class TestCostPerSuccessfulTask:
    def test_it_divides_by_the_tasks_that_passed(self) -> None:
        sims = [simulation("0", reward=1.0), simulation("1", reward=0.0)]
        rewards = rewards_by_task(sims)
        assert passed_count(rewards) == 1
        assert per_successful_task(10.0, 1) == 10.0

    def test_no_success_leaves_it_undefined_rather_than_infinite(self) -> None:
        assert per_successful_task(10.0, 0) is None

    def test_an_unscored_task_is_not_a_failure(self) -> None:
        """Upstream drops infrastructure failures from the denominator. Counting them as
        zero here would report a different score from the run's own."""
        sims = [simulation("0", reward=1.0), simulation("1", reward=None)]
        assert list(rewards_by_task(sims)) == ["0"]


BASELINE_RUN = REPO / "evaluation" / "results" / "20260908T121800Z_deepseek-v4-pro_full_guarded"


class TestAgainstTheRunThatExposedIt:
    """The `baseline-v3` run, if its raw results are on this machine.

    Only summary.json is version-controlled; the conversation traces are too large, so
    this skips on a fresh clone. It is kept because it is the only test that exercises
    the whole path on real data, and it is the exact case that used to disagree with
    upstream's accounting by a factor of two.
    """

    @pytest.fixture
    def simulations(self) -> list[dict[str, Any]]:
        from retail_support_agent.results_io import load_simulations, result_files

        if not BASELINE_RUN.is_dir() or not result_files(BASELINE_RUN):
            pytest.skip("raw simulations are not version-controlled; run the evaluation first")
        return load_simulations(BASELINE_RUN)

    def test_the_agent_cost_equals_upstreams_own_accounting(
        self, simulations: list[dict[str, Any]]
    ) -> None:
        run_summary = json.loads((BASELINE_RUN / "summary.json").read_text(encoding="utf-8"))
        upstream = run_summary["results"]["agent_cost_reported_by_upstream"]
        clock = recover_clock(run_summary, simulations)
        calls = model_calls(simulations, clock, role="assistant")
        cost = run_cost(
            str(run_summary["stack"]["agent_model"]),
            calls,
            fallback=run_started_at(run_summary),
        )
        assert cost.total == pytest.approx(upstream, rel=1e-6)

    def test_the_run_is_off_peak_throughout(self, simulations: list[dict[str, Any]]) -> None:
        """It ran on a Tuesday evening in Beijing. Reading it during a peak window used
        to double the reported figure."""
        run_summary = json.loads((BASELINE_RUN / "summary.json").read_text(encoding="utf-8"))
        clock = recover_clock(run_summary, simulations)
        calls = model_calls(simulations, clock, role="assistant")
        cost = run_cost("deepseek-v4-pro", calls, fallback=run_started_at(run_summary))
        assert cost.tiers_used == [Tier.OFF_PEAK]
