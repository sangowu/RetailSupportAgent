"""Price snapshot and cost computation.

Upstream accounts through litellm, and its `agent_cost` came out as 0 for every call.
Cost is therefore computed here from token usage, with the date the prices were read.

(Corrected 2026-09-07: this file used to say "litellm does not know these prices",
which was half right. The real reason is in the `register_with_litellm` notes below:
mainly the `openai/` prefix we send makes the lookup fail, and only secondarily are
three models genuinely absent. After registration both causes are gone and
`agent_cost` equals this module's computation exactly.)

Three things that were initially ignored and measurably change the conclusion:

1. Cache hits. DashScope implicit caching is on by default and was already active: in
   the smoke run 93312 of 129262 agent-side input tokens were cache hits (72%).
   Charging full price overstates cost.
2. Reasoning tokens. The simulated user produced 4451 reasoning tokens out of 4980
   output tokens, billed at the output rate and previously not counted at all.
3. The judge runs on a different provider, and DeepSeek has peak/off-peak pricing:
   the same call costs twice as much during peak hours.

Prices change. Every number carries the date it was read and should be re-checked
once stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

PRICE_SNAPSHOT_DATE = "2026-09-06"

# Implicit cache hits are billed at 20% of the input rate (Alibaba Cloud Context Cache
# documentation). The docs disagree with each other: the pricing page says "cache hit
# 10%", the Context Cache page says "explicit 10% / implicit 20%". This project uses
# implicit caching, so 20% applies, which is also the more expensive reading. Better to
# overstate cost than to understate it in a report.
DASHSCOPE_IMPLICIT_CACHE_RATIO = 0.20

# Three models were once listed here as "cache discount unpublished". On 2026-09-06
# their rates were found, and the cost range collapsed to an exact number. The empty
# set is kept so the mechanism survives: adding a model here switches its cost back to
# a range rather than a falsely precise figure.
DASHSCOPE_CACHE_RATIO_UNKNOWN: frozenset[str] = frozenset()

# Cache-hit rates read from the provider (CNY per million tokens); these take
# precedence over the ratio. Source: Qwen model pages and the Alibaba Cloud Bailian
# pricing page, verified 2026-09-06.
DASHSCOPE_CACHE_PRICE_OVERRIDES: dict[str, float] = {
    "qwen3.8-max": 1.5,
    "qwen3.8-flash": 0.1,
    "deepseek-v4-pro-0813": 1.2,
}


class Tier(StrEnum):
    """DeepSeek pricing tiers. Off-peak is half the peak rate."""

    PEAK = "peak"
    OFF_PEAK = "off_peak"


@dataclass(frozen=True)
class Price:
    """CNY per million tokens.

    `completion_tokens` already includes `reasoning_tokens`; do not add them twice.
    Measured on qwen3.6-plus 2026-09-06: prompt 32 + completion 2865 = total 2897,
    with completion_tokens_details.reasoning_tokens = 2709, and 2865 - 2709 = 156,
    exactly the length of the visible reply. DeepSeek behaves the same.
    (Note `completion_tokens_details.text_tokens` measured equal to completion_tokens
    itself, not to the visible part, so it cannot be used for that subtraction.)

    `cached_input = None` means the cache-hit rate for that model is unknown, and cost
    can then only be given as a range rather than a falsely precise number.
    """

    input: float
    output: float
    cached_input: float | None = None

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
        """With an unknown cache rate, charge full price: overstate rather than understate."""
        cached = min(cached_tokens, prompt_tokens)
        uncached = prompt_tokens - cached
        cached_rate = self.input if self.cached_input is None else self.cached_input
        return (
            uncached / 1e6 * self.input
            + cached / 1e6 * cached_rate
            + completion_tokens / 1e6 * self.output
        )

    def cost_floor(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int) -> float:
        """Lower bound when the cache rate is unknown, assuming hits cost 20%.

        Used only to present a range.
        """
        if self.cached_input is not None:
            return self.cost(prompt_tokens, completion_tokens, cached_tokens)
        cached = min(cached_tokens, prompt_tokens)
        return (
            (prompt_tokens - cached) / 1e6 * self.input
            + cached / 1e6 * self.input * DASHSCOPE_IMPLICIT_CACHE_RATIO
            + completion_tokens / 1e6 * self.output
        )


def _dashscope(input_price: float, output_price: float, model: str) -> Price:
    if model in DASHSCOPE_CACHE_RATIO_UNKNOWN:
        cached = None
    else:
        cached = DASHSCOPE_CACHE_PRICE_OVERRIDES.get(
            model, input_price * DASHSCOPE_IMPLICIT_CACHE_RATIO
        )
    return Price(input=input_price, output=output_price, cached_input=cached)


# Source https://help.aliyun.com/zh/model-studio/model-pricing, Beijing region,
# verified 2026-09-06. qwen3.x-max rates are for non-thinking mode; thinking-mode
# rates were not available.
DASHSCOPE_PRICES: dict[str, Price] = {
    "qwen3.7-max-2026-06-08": _dashscope(12.0, 36.0, "qwen3.7-max-2026-06-08"),
    "qwen3.8-max": _dashscope(12.0, 36.0, "qwen3.8-max"),
    "qwen3.8-flash": _dashscope(0.8, 2.7, "qwen3.8-flash"),
    "deepseek-v4-pro-0813": _dashscope(12.0, 24.0, "deepseek-v4-pro-0813"),
    "glm-5.2": _dashscope(8.0, 28.0, "glm-5.2"),
    # The simulated user, part of the frozen evaluation stack. It was missing from the
    # snapshot, which made it the one role with no computable cost, while it produces
    # about 85k output tokens per 24 tasks, 94% of them reasoning, billed at the
    # output rate.
    #
    # 2/12 are the rates for the <=256K input tier. Bailian prices some models by
    # request input length, but this role's input peaks at 2096 tokens and always falls
    # in the lowest tier, so a flat rate holds. Using it for long-context work would
    # require re-tiering.
    #
    # Cache: explicit create 2.5 (125% of input), explicit hit 0.2 (10%); this project
    # uses implicit caching, so 20% = 0.4. Implicit caching has a 1024-token minimum
    # while this role's median input is 848: of 120 measured calls only 43 reached the
    # threshold and the server never reported a hit, so this rate is currently never
    # applied and everything is charged at the input rate, which is conservative.
    "qwen3.6-plus-2026-04-02": Price(input=2.0, output=12.0, cached_input=0.4),
    # The judge model. Lower source confidence than the rows above: the official
    # pricing page loads via JavaScript and could not be scraped, so these three
    # numbers come from secondary reporting (input 0.1 / output 0.4 / cache 0.01 CNY
    # per million tokens). Judge cost is negligible (a full re-judge is about 0.07
    # CNY) and cannot change any model-selection conclusion, but any external report
    # quoting it must first reconcile against the console bill.
    "qwen3.7-flash-2026-07-15": Price(input=0.1, output=0.4, cached_input=0.01),
}

# Source https://api-docs.deepseek.com/zh-cn/quick_start/pricing, verified 2026-09-06.
# These are DeepSeek's own endpoint rates and differ from the same-named model on
# DashScope.
DEEPSEEK_PRICES: dict[tuple[str, Tier], Price] = {
    ("deepseek-v4-flash", Tier.PEAK): Price(input=3.0, output=9.0, cached_input=0.10),
    ("deepseek-v4-flash", Tier.OFF_PEAK): Price(input=1.5, output=4.5, cached_input=0.05),
    ("deepseek-v4-pro", Tier.PEAK): Price(input=9.0, output=27.0, cached_input=0.30),
    ("deepseek-v4-pro", Tier.OFF_PEAK): Price(input=4.5, output=13.5, cached_input=0.15),
}

# Zhipu open platform, bigmodel.cn (the mainland China service). Verified 2026-09-06.
# Distinct from z.ai, which prices in USD (GLM-5.3-Flash $0.075/$0.25) and uses a
# different endpoint (api.z.ai vs open.bigmodel.cn); the two price tables must not be
# mixed. GLM-5.3-Flash currently has a limited-time 50% discount; the list price is
# recorded here, because a promotional price in a snapshot expires silently.
ZHIPU_PRICES: dict[str, Price] = {
    "glm-5.3-flash": Price(input=0.8, output=2.8, cached_input=0.23),
}

# Peak: Beijing time Monday to Friday 09:00-12:00 and 14:00-18:00
# (= UTC 01:00-04:00 and 06:00-10:00).
_PEAK_WINDOWS_BEIJING = ((9, 12), (14, 18))
_BEIJING_OFFSET = timedelta(hours=8)


def deepseek_tier(moment: datetime | None = None) -> Tier:
    """Whether a moment falls in DeepSeek's peak or off-peak window."""
    moment = moment or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    beijing = moment.astimezone(UTC) + _BEIJING_OFFSET
    if beijing.weekday() >= 5:  # weekends are off-peak all day
        return Tier.OFF_PEAK
    return (
        Tier.PEAK
        if any(start <= beijing.hour < end for start, end in _PEAK_WINDOWS_BEIJING)
        else Tier.OFF_PEAK
    )


def price_for(model: str, moment: datetime | None = None) -> Price | None:
    """Look up a price by model name. DeepSeek's own endpoint varies by time of day."""
    if model in DASHSCOPE_PRICES:
        return DASHSCOPE_PRICES[model]
    if model in ZHIPU_PRICES:
        return ZHIPU_PRICES[model]
    return DEEPSEEK_PRICES.get((model, deepseek_tier(moment)))


def cache_ratio_is_known(model: str) -> bool:
    price = DASHSCOPE_PRICES.get(model) or ZHIPU_PRICES.get(model)
    if price is not None:
        return price.cached_input is not None
    return any(key[0] == model for key in DEEPSEEK_PRICES)


# ---------------------------------------------------------------------------
# litellm price registration
# ---------------------------------------------------------------------------
#
# Upstream accounts through `completion_cost()`. It raised on every call here, and
# `get_response_cost()` caught the exception and returned 0.0, which is why
# `agent_cost_reported_by_upstream` in summary.json was always 0.
#
# The real cause, established 2026-09-07, differs from what was documented before:
#
#   litellm 1.82.6 does know `deepseek/deepseek-v4-pro` (1.32/3.96 USD per million
#   tokens), but the model name sent here carries an `openai/` prefix (an
#   OpenAI-compatible endpoint), so litellm looks it up with
#   custom_llm_provider=openai and reports "This model isn't mapped yet.
#   model=deepseek-v4-pro, custom_llm_provider=openai".
#
#   The other three (qwen3.7-flash / qwen3.6-plus / glm-5.3-flash) are genuinely absent.
#
# Both causes need fixing: register under the key actually sent, and register all four
# models.
#
# ## Currency: CNY is registered, not USD
#
# litellm's built-in prices are USD and the Langfuse UI renders a dollar sign. CNY is
# registered anyway, because verifiability matters more than the symbol:
#
#   - CNY is the unit of every cost report in this project (analyze_eval.py, docs,
#     PRs). Registering CNY makes litellm's number equal this module's number exactly,
#     which a test pins down (tests/test_litellm_pricing.py).
#   - Converting to USD would introduce an exchange-rate assumption. Rates change
#     daily and a snapshot does not update itself, which is an error that expires
#     silently, worse than a clearly labelled wrong symbol.
#
# So: any cost number coming from litellm or Langfuse is in CNY. Field names and
# documentation must say so, so nobody reads it as USD.
LITELLM_COST_CURRENCY = "CNY"

# The model-name prefix sent to litellm (OpenAI-compatible endpoint). Matches
# evaluation.litellm_model.
LITELLM_PREFIX = "openai/"

# One litellm cost entry. Values are either a rate (float) or a provider/mode (str).
# Not `object`: that would force a cast before every arithmetic operation and hide
# type errors instead of exposing them.
CostEntry = dict[str, float | str]


def _cost_entry(price: Price, model: str) -> CostEntry:
    """Translate one price into a litellm cost entry. Units are CNY per token."""
    entry: CostEntry = {
        "litellm_provider": "openai",
        "mode": "chat",
        "input_cost_per_token": price.input / 1e6,
        "output_cost_per_token": price.output / 1e6,
    }
    if price.cached_input is not None:
        # Both field names are needed: different litellm code paths read different ones.
        entry["cache_read_input_token_cost"] = price.cached_input / 1e6
        entry["input_cost_per_token_cache_hit"] = price.cached_input / 1e6
    return entry


def litellm_model_cost(moment: datetime | None = None) -> dict[str, CostEntry]:
    """The litellm cost table for this snapshot, keyed by the model name actually sent
    (with the openai/ prefix).

    DeepSeek prices vary by tier, so this table is a snapshot of one moment. A long run
    crossing a tier boundary is accounted at the tier registered at start, which can
    differ from analyze_eval's per-run-time computation. That is the granularity of
    DeepSeek's pricing rule, not a defect here.
    """
    table: dict[str, CostEntry] = {}
    for model, price in DASHSCOPE_PRICES.items():
        table[f"{LITELLM_PREFIX}{model}"] = _cost_entry(price, model)
    for model, price in ZHIPU_PRICES.items():
        table[f"{LITELLM_PREFIX}{model}"] = _cost_entry(price, model)
    tier = deepseek_tier(moment)
    for (model, model_tier), price in DEEPSEEK_PRICES.items():
        if model_tier is tier:
            table[f"{LITELLM_PREFIX}{model}"] = _cost_entry(price, model)
    return table


def register_with_litellm(moment: datetime | None = None) -> dict[str, CostEntry]:
    """Register the price snapshot with litellm and return what was registered, for
    the run manifest.

    Called only when actually running an evaluation: litellm is a heavy bench-group
    dependency and importing `pricing` must not pull it in.
    """
    import litellm

    table = litellm_model_cost(moment)
    litellm.register_model(table)
    return table
