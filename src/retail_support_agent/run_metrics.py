"""Cost and latency of an evaluation run, computed from its own raw results.

Two measurements that M5 needs and that the repository did not have.

## Cost, priced by the run's clock rather than the reader's

DeepSeek's own endpoint charges a peak and an off-peak rate differing by exactly two,
and `pricing.price_for` defaults its moment to now. Every cost figure printed about a
past run was therefore the cost those tokens would have had if bought at the moment of
reading. Measured on the `baseline-v3` run: 20.3054 CNY printed at peak against
10.152675 CNY actually spent off-peak, a mismatch the analysis script reported and could
not explain.

Calls are priced one at a time, at the moment each was made. Not at the run's start
either: a full run takes about 1.7 hours and the peak windows are 3 and 4 hours long, so
a run can straddle a boundary, and per-call pricing needs no exception written for it.

## The clock the moments come from

Message timestamps in the raw results are naive local time from the machine that ran the
evaluation; `run_id` is UTC by construction. The offset between them is recovered from
those two facts alone and checked against the run's wall time, so the analysis machine's
own timezone never enters. New runs record `local_utc_offset_seconds` in summary.json and
skip the recovery; it exists for the runs already on disk.

A run whose offset cannot be recovered, or does not survive the check, is priced at the
tier of its start and says so. Guessing a moment is exactly the failure being fixed.

## Latency

`generation_time_seconds` is recorded per assistant message and had never been read. It
is the agent's own model call, which is the part of the wall clock this system is
responsible for. Task duration also contains the simulated user and is inflated by
running two tasks at once, so it is reported under its own name and never called latency.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from retail_support_agent.pricing import (
    DEEPSEEK_PRICES,
    Price,
    Tier,
    deepseek_tier,
    price_for,
)

# Timezone offsets are whole quarter hours, so rounding the difference between the run
# id and the first message to 15 minutes absorbs the setup time between the two without
# ever landing on an offset no timezone uses.
_QUARTER_HOUR = 900
# How far the gap between the run id and the first message may sit from a whole quarter
# hour before the offset is rejected. The gap is one task's setup, measured at under a
# second on this project's runs; five minutes is generous and still an order of magnitude
# below the smallest offset that would change a price tier.
_MAX_RESIDUAL = timedelta(minutes=5)
# Slack on the other end, where the last message is compared with the recorded wall time.
_CLOCK_SLACK = timedelta(minutes=10)


# ---------------------------------------------------------------------------
# usage extraction
# ---------------------------------------------------------------------------


def usage_of(message: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer the full response in raw_data: it carries cache and reasoning detail that
    upstream's usage field does not."""
    raw = message.get("raw_data")
    if isinstance(raw, dict):
        raw_usage = raw.get("usage")
        if isinstance(raw_usage, dict):
            return dict(raw_usage)
    usage = message.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


def cached_tokens_of(usage: dict[str, Any]) -> int | None:
    """Cache hits, or None when the server did not report the field at all.

    Three field names can appear: the flat field added by our patch, OpenAI's nested
    detail, and DeepSeek's prompt_cache_hit_tokens. Only if all are absent is it
    counted as unreported.
    """
    for key in ("cached_tokens", "prompt_cache_hit_tokens"):
        value = usage.get(key)
        if value is not None:
            return int(value)
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details["cached_tokens"])
    return None


def reasoning_tokens_of(usage: dict[str, Any]) -> int:
    details = usage.get("completion_tokens_details")
    nested = int((details or {}).get("reasoning_tokens") or 0) if isinstance(details, dict) else 0
    return int(usage.get("reasoning_tokens") or 0) or nested


# ---------------------------------------------------------------------------
# the run's clock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunClock:
    """How to read this run's naive message timestamps as UTC.

    `offset` is what the recording machine was ahead of UTC, so `utc = naive - offset`.
    When it is None the timestamps cannot be placed on a timeline at all, and callers
    must fall back to the run's start rather than treating them as UTC.
    """

    offset: timedelta | None
    source: str  # "recorded" | "recovered" | "unavailable"
    note: str = ""

    def to_utc(self, naive: datetime) -> datetime | None:
        if self.offset is None:
            return None
        if naive.tzinfo is not None:
            return naive.astimezone(UTC)
        return (naive - self.offset).replace(tzinfo=UTC)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def run_started_at(summary: dict[str, Any]) -> datetime | None:
    """The run's start in UTC, taken from the run id.

    `run_at` in summary.json is written when the run finishes, so it is the end, not the
    start. The run id is `datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")` and is the only
    unambiguous UTC moment in the file.
    """
    run_id = summary.get("run_id")
    if not isinstance(run_id, str):
        return None
    try:
        return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def message_timestamps(simulations: Iterable[dict[str, Any]]) -> list[datetime]:
    out: list[datetime] = []
    for sim in simulations:
        for message in sim.get("messages") or []:
            moment = _parse(message.get("timestamp"))
            if moment is not None:
                out.append(moment)
    return out


def recover_clock(summary: dict[str, Any], simulations: Sequence[dict[str, Any]]) -> RunClock:
    """Work out the offset between the run's naive timestamps and UTC.

    Recorded offsets are trusted. Otherwise: the first message of a run is the opening
    line of its first task, generated locally moments after the run id is taken, so the
    gap between the two is the offset plus a few seconds of setup. It is rounded to a
    quarter hour, and the leftover has to be small - a gap that is not close to a whole
    quarter hour is not a timezone, and rounding it would invent an hour.

    Messages reaching past the recorded wall time do not reject the offset. That is what
    a resume looks like: it writes into the same directory hours or days later and never
    updates summary.json. It is worth a note, not a refusal to price the run.
    """
    recorded = summary.get("local_utc_offset_seconds")
    if isinstance(recorded, int | float):
        return RunClock(timedelta(seconds=float(recorded)), "recorded")

    started = run_started_at(summary)
    stamps = message_timestamps(simulations)
    if started is None or not stamps:
        return RunClock(None, "unavailable", "no run id or no timestamped message")
    if any(stamp.tzinfo is not None for stamp in stamps):
        return RunClock(timedelta(0), "recorded", "timestamps already carry a timezone")

    raw = (min(stamps) - started.replace(tzinfo=None)).total_seconds()
    offset = timedelta(seconds=round(raw / _QUARTER_HOUR) * _QUARTER_HOUR)

    residual = abs(timedelta(seconds=raw) - offset)
    if residual > _MAX_RESIDUAL:
        return RunClock(
            None,
            "unavailable",
            f"the first message is {raw / 3600:.2f}h after the run id, which is "
            f"{residual.total_seconds() / 60:.0f} minutes from any whole quarter hour",
        )

    wall = summary.get("wall_seconds")
    span = timedelta(seconds=float(wall)) if isinstance(wall, int | float) else timedelta(0)
    end = (max(stamps) - offset).replace(tzinfo=UTC)
    if span and end > started + span + _CLOCK_SLACK:
        over = (end - started - span).total_seconds() / 3600
        return RunClock(
            offset,
            "recovered",
            f"the last message is {over:.1f}h past the recorded wall time, which is what "
            "a resumed run looks like; the calls are still priced at their own moments",
        )
    return RunClock(offset, "recovered")


# ---------------------------------------------------------------------------
# model calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCall:
    """One call to a model, as recorded in the raw results."""

    task: str
    role: str
    moment: datetime | None
    prompt: int
    completion: int
    cached: int
    cache_reported: bool
    reasoning: int
    seconds: float | None

    def cost(self, moment_price: Price | None, *, floor: bool = False) -> float | None:
        """`floor` is the lower bound used when a model's cache-hit rate is unpublished,
        so the report can give a range instead of a falsely precise number."""
        if moment_price is None:
            return None
        if floor:
            return moment_price.cost_floor(self.prompt, self.completion, self.cached)
        return moment_price.cost(self.prompt, self.completion, self.cached)


def model_calls(
    simulations: Iterable[dict[str, Any]], clock: RunClock, role: str | None = None
) -> list[ModelCall]:
    """Every message that carries a usage record, as a priceable, timeable call.

    Messages without usage are not calls: the opening line of each task is generated
    locally and reaches no model.

    Each call keeps its task id, so a paired comparison can charge two runs for the same
    tasks rather than for whichever tasks each of them happened to complete.
    """
    calls: list[ModelCall] = []
    for sim in simulations:
        for message in sim.get("messages") or []:
            this_role = str(message.get("role", "unknown"))
            if role is not None and this_role != role:
                continue
            usage = usage_of(message)
            if usage is None:
                continue
            naive = _parse(message.get("timestamp"))
            cached = cached_tokens_of(usage)
            seconds = message.get("generation_time_seconds")
            calls.append(
                ModelCall(
                    task=str(sim.get("task_id")),
                    role=this_role,
                    moment=clock.to_utc(naive) if naive is not None else None,
                    prompt=int(usage.get("prompt_tokens") or 0),
                    completion=int(usage.get("completion_tokens") or 0),
                    cached=int(cached or 0),
                    cache_reported=cached is not None,
                    reasoning=reasoning_tokens_of(usage),
                    seconds=float(seconds) if isinstance(seconds, int | float) else None,
                )
            )
    return calls


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


def price_varies_with_time(model: str) -> bool:
    """Whether this model's rate depends on when the call was made."""
    return any(key[0] == model for key in DEEPSEEK_PRICES)


# Fixed moments used to price a run "as if" it had run entirely in one tier, so two
# configurations can be compared on their tokens rather than on the hour they were run.
# Both are a Monday: the weekend is off-peak all day, so a weekend reference would make
# the peak moment unreachable. A test pins that each really falls in its own tier.
_REFERENCE_MOMENTS: dict[Tier, datetime] = {
    Tier.PEAK: datetime(2026, 1, 5, 2, 0, tzinfo=UTC),  # Beijing 10:00, inside 09-12
    Tier.OFF_PEAK: datetime(2026, 1, 5, 12, 0, tzinfo=UTC),  # Beijing 20:00
}


def reference_moment(tier: Tier) -> datetime:
    """A moment that is always in `tier`, for pricing a run at one stated rate."""
    return _REFERENCE_MOMENTS[tier]


@dataclass
class RunCost:
    """What a run cost, and under which clock the answer was reached."""

    total: float = 0.0
    priced_calls: int = 0
    unpriced_calls: int = 0
    undated_calls: int = 0
    by_tier: dict[Tier, int] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        """False when no call could be priced. Zero is a cost; unknown is not."""
        return self.priced_calls > 0

    @property
    def tiers_used(self) -> list[Tier]:
        return [tier for tier, count in sorted(self.by_tier.items()) if count]


def run_cost(
    model: str,
    calls: Sequence[ModelCall],
    *,
    fallback: datetime | None = None,
    tier_moment: datetime | None = None,
    floor: bool = False,
) -> RunCost:
    """Total cost of these calls.

    `tier_moment` fixes one moment for every call, which is what a comparison between two
    configurations wants: the same tokens at the same rates. Left None, each call is
    priced at its own moment, which is what the run actually cost. `fallback` prices the
    calls whose moment could not be established, and is normally the run's start.

    No price is ever looked up at the current time. A call with no moment at all, for a
    model whose rate depends on the moment, is counted as unpriced rather than charged at
    whatever the rate happens to be while the report is being read.
    """
    out = RunCost()
    for call in calls:
        moment = tier_moment or call.moment or fallback
        if call.moment is None and tier_moment is None:
            out.undated_calls += 1
        if moment is None and price_varies_with_time(model):
            # No moment and a rate that depends on the moment. Falling back to the
            # current time is the defect this module exists to remove, so the call is
            # reported as unpriced instead. Unpriced is not free, and the caller says so.
            out.unpriced_calls += 1
            continue
        price = price_for(model, moment)
        if price is None:
            out.unpriced_calls += 1
            continue
        amount = call.cost(price, floor=floor)
        if amount is None:  # pragma: no cover - price is not None here
            out.unpriced_calls += 1
            continue
        out.total += amount
        out.priced_calls += 1
        if price_varies_with_time(model) and moment is not None:
            tier = deepseek_tier(moment)
            out.by_tier[tier] = out.by_tier.get(tier, 0) + 1
    return out


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile.

    No interpolation: with a few hundred calls, interpolating between two measured
    latencies reports a number that was never observed, and a P95 is quoted as an
    observation.
    """
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True)
class Distribution:
    """A measured distribution, reported as observations rather than a mean alone."""

    count: int
    p50: float
    p95: float
    maximum: float
    mean: float
    total: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Distribution | None:
        if not values:
            return None
        return cls(
            count=len(values),
            p50=percentile(values, 50),
            p95=percentile(values, 95),
            maximum=max(values),
            mean=sum(values) / len(values),
            total=sum(values),
        )


def call_latencies(calls: Iterable[ModelCall]) -> list[float]:
    return [call.seconds for call in calls if call.seconds is not None]


def task_durations(simulations: Iterable[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for sim in simulations:
        duration = sim.get("duration")
        if isinstance(duration, int | float) and duration > 0:
            out.append(float(duration))
    return out


@dataclass(frozen=True)
class LatencyProfile:
    """Agent-side latency, and the wall clock it sits inside.

    The two are kept apart on purpose. `per_call` is this system's own responsibility.
    `per_task` is the harness: it contains the simulated user's own thinking and tool
    execution, and it is inflated by however many tasks ran at once.
    """

    per_call: Distribution | None
    per_task: Distribution | None
    calls_without_timing: int

    @property
    def model_share(self) -> float | None:
        """Share of task wall time spent inside the agent's own model calls."""
        if self.per_call is None or self.per_task is None or self.per_task.total <= 0:
            return None
        return self.per_call.total / self.per_task.total


def latency_profile(
    calls: Sequence[ModelCall], simulations: Sequence[dict[str, Any]]
) -> LatencyProfile:
    timed = call_latencies(calls)
    return LatencyProfile(
        per_call=Distribution.of(timed),
        per_task=Distribution.of(task_durations(simulations)),
        calls_without_timing=len(calls) - len(timed),
    )


# ---------------------------------------------------------------------------
# quality, so that cost per successful task can be divided by something
# ---------------------------------------------------------------------------


def rewards_by_task(simulations: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Task id to reward. Unscored simulations are excluded, never counted as zero."""
    out: dict[str, float] = {}
    for sim in simulations:
        info = sim.get("reward_info") or {}
        if info.get("reward") is not None:
            out[str(sim.get("task_id"))] = float(info["reward"])
    return out


def passed_count(rewards: dict[str, float]) -> int:
    return sum(1 for reward in rewards.values() if reward >= 1.0)


def per_successful_task(total: float, passes: int) -> float | None:
    """Cost divided by the tasks that actually passed.

    None when nothing passed: dividing by zero successes would report a configuration
    that fails everything as infinitely expensive, which is arithmetically true and
    practically a crash.
    """
    return total / passes if passes else None
