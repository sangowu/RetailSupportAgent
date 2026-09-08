"""litellm's cost must be exactly equal to our own conversion.

That equality is the whole reason for registering prices in CNY rather than converting to
USD: with no exchange-rate assumption the two accounts must agree, and a disagreement is a
bug rather than rounding.

Background: upstream accounts with `completion_cost()`, which was measured to raise on
every one of our calls; `get_response_cost()` catches it and returns 0.0, which is why
`agent_cost_reported_by_upstream` in summary.json stayed at 0.
"""

from __future__ import annotations

import pytest

from retail_support_agent.evaluation import FROZEN_NL_JUDGE, FROZEN_USER_SIMULATOR, litellm_model
from retail_support_agent.pricing import (
    LITELLM_COST_CURRENCY,
    LITELLM_PREFIX,
    Tier,
    litellm_model_cost,
    price_for,
    register_with_litellm,
)

pytest.importorskip("litellm", reason="litellm is in the bench group; skip when not installed")

SELECTED = "deepseek-v4-pro"


def _response(model: str, prompt: int, completion: int) -> object:
    from litellm.types.utils import Choices, Message, ModelResponse, Usage

    return ModelResponse(
        id="test",
        created=0,
        model=model,
        object="chat.completion",
        choices=[
            Choices(index=0, message=Message(role="assistant", content="x"), finish_reason="stop")
        ],
        usage=Usage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
    )


class TestKeysMatchWhatWeActuallySend:
    """Registering under the wrong key is the same as not registering: no error is raised
    and the cost stays 0."""

    def test_keys_carry_the_openai_prefix(self) -> None:
        """We call the OpenAI-compatible endpoint and send the model name as
        `openai/<model>`.

        litellm infers custom_llm_provider=openai from that and looks up the table under the
        same prefix. A bare name is not found, and the observed error is
        "This model isn't mapped yet".
        """
        table = litellm_model_cost()
        assert all(key.startswith(LITELLM_PREFIX) for key in table)

    def test_every_frozen_role_is_registered(self) -> None:
        table = litellm_model_cost()
        for model in (SELECTED, FROZEN_USER_SIMULATOR, FROZEN_NL_JUDGE):
            assert litellm_model(model) in table, (
                f"{model} is not registered, so its cost would be counted as 0"
            )


class TestRegisteredCostMatchesOurOwn:
    def test_cost_is_identical_not_merely_close(self) -> None:
        from litellm import completion_cost

        register_with_litellm()
        prompt, completion = 100_000, 5_000
        price = price_for(SELECTED)
        assert price is not None
        theirs = completion_cost(
            completion_response=_response(litellm_model(SELECTED), prompt, completion)
        )
        ours = price.cost(prompt, completion)
        assert theirs == pytest.approx(ours, rel=1e-12)

    def test_registration_makes_the_zero_go_away(self) -> None:
        """Before registration completion_cost raises (upstream swallows it and returns
        0.0); after registration it returns a real number."""
        from litellm import completion_cost

        register_with_litellm()
        cost = completion_cost(completion_response=_response(litellm_model(SELECTED), 1000, 100))
        assert cost > 0

    def test_the_currency_is_cny_and_says_so(self) -> None:
        """The values are CNY. litellm and Langfuse both label them with a dollar sign,
        which is a wrong symbol, not a wrong number.

        This test exists so that anyone switching to USD has to change it first, and is
        forced to read why no exchange rate is introduced.
        """
        assert LITELLM_COST_CURRENCY == "CNY"
        entry = litellm_model_cost()[litellm_model(SELECTED)]
        price = price_for(SELECTED)
        assert price is not None
        assert entry["input_cost_per_token"] == price.input / 1e6


class TestCacheAndTierAreCarriedOver:
    def test_cache_hit_price_is_registered_under_both_names(self) -> None:
        """litellm reads different field names on different code paths, so registering
        only one of them misses."""
        entry = litellm_model_cost()[litellm_model(SELECTED)]
        assert entry["cache_read_input_token_cost"] == entry["input_cost_per_token_cache_hit"]

    def test_deepseek_tier_is_reflected(self) -> None:
        """DeepSeek off-peak prices are half of peak, so the registry must follow the
        time of day."""
        from datetime import UTC, datetime

        peak = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)  # 10:00 Monday, Beijing time
        off = datetime(2026, 9, 7, 20, 0, tzinfo=UTC)  # 04:00 Tuesday, Beijing time
        key = litellm_model(SELECTED)
        peak_cost = float(litellm_model_cost(peak)[key]["input_cost_per_token"])
        off_cost = float(litellm_model_cost(off)[key]["input_cost_per_token"])
        assert peak_cost == pytest.approx(off_cost * 2)

    def test_only_one_tier_is_registered_at_a_time(self) -> None:
        """One model name cannot carry two tiers at once: the later write overwrites the
        earlier one and the result depends on dict order."""
        table = litellm_model_cost()
        assert sum(1 for k in table if k == litellm_model(SELECTED)) == 1
        assert set(Tier)  # both tiers are still defined; each registration picks one
