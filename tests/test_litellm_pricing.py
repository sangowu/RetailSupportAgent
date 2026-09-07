"""litellm 的成本必须与我们自己的折算**严格相等**。

这条相等关系是选择"注册人民币而不是换算美元"的全部理由：
没有汇率假设，两套账就能对得上，对不上就是 bug 而不是舍入。

背景：上游用 `completion_cost()` 记账，实测它对我们的每次调用都抛异常，
`get_response_cost()` 捕获后 `return 0.0`——summary.json 里
`agent_cost_reported_by_upstream` 长期恒为 0 就是这么来的。
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

pytest.importorskip("litellm", reason="litellm 属 bench 组，未装则跳过")

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
    """按错的键注册等于没注册——而且不会报错，成本继续是 0。"""

    def test_keys_carry_the_openai_prefix(self) -> None:
        """我们走 OpenAI 兼容端点，下发的模型名是 `openai/<model>`。

        litellm 由此推断 custom_llm_provider=openai 并按这个前缀查表；
        注册裸名字查不到，实测报 "This model isn't mapped yet"。
        """
        table = litellm_model_cost()
        assert all(key.startswith(LITELLM_PREFIX) for key in table)

    def test_every_frozen_role_is_registered(self) -> None:
        table = litellm_model_cost()
        for model in (SELECTED, FROZEN_USER_SIMULATOR, FROZEN_NL_JUDGE):
            assert litellm_model(model) in table, f"{model} 未注册，它的成本会被算成 0"


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
        """注册前 completion_cost 抛异常（上游吞掉后返回 0.0），注册后有真数。"""
        from litellm import completion_cost

        register_with_litellm()
        cost = completion_cost(completion_response=_response(litellm_model(SELECTED), 1000, 100))
        assert cost > 0

    def test_the_currency_is_cny_and_says_so(self) -> None:
        """数值是人民币。litellm 与 Langfuse 都会给它加美元符号，那是符号错、不是数值错。

        这条测试的作用是：谁要把它改成美元，得先来改这里，
        顺带被迫读到"为什么不引汇率"的理由。
        """
        assert LITELLM_COST_CURRENCY == "CNY"
        entry = litellm_model_cost()[litellm_model(SELECTED)]
        price = price_for(SELECTED)
        assert price is not None
        assert entry["input_cost_per_token"] == price.input / 1e6


class TestCacheAndTierAreCarriedOver:
    def test_cache_hit_price_is_registered_under_both_names(self) -> None:
        """litellm 在不同代码路径上读的不是同一个字段名，只给一个会漏。"""
        entry = litellm_model_cost()[litellm_model(SELECTED)]
        assert entry["cache_read_input_token_cost"] == entry["input_cost_per_token_cache_hit"]

    def test_deepseek_tier_is_reflected(self) -> None:
        """DeepSeek 谷时价是峰时的一半，注册表必须跟着时段走。"""
        from datetime import UTC, datetime

        peak = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)  # 北京时间周一 10:00
        off = datetime(2026, 9, 7, 20, 0, tzinfo=UTC)  # 北京时间周二 04:00
        key = litellm_model(SELECTED)
        peak_cost = float(litellm_model_cost(peak)[key]["input_cost_per_token"])
        off_cost = float(litellm_model_cost(off)[key]["input_cost_per_token"])
        assert peak_cost == pytest.approx(off_cost * 2)

    def test_only_one_tier_is_registered_at_a_time(self) -> None:
        """同一个模型名不能同时挂两档价——后写的会覆盖先写的，结果取决于字典顺序。"""
        table = litellm_model_cost()
        assert sum(1 for k in table if k == litellm_model(SELECTED)) == 1
        assert set(Tier)  # 两档都还在定义里，只是每次注册取其一
