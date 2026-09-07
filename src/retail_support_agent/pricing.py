"""价格快照与成本折算。

上游用 litellm 记账，而它算出来的 `agent_cost` 一律是 0。成本必须自己按 token
用量折算，并**注明取价日期**。

（2026-09-07 更正：此前这里写的是"litellm 不认识这些价格"，只对了一半。
真正的原因见下方 `register_with_litellm` 的注释——主要是我们下发的 `openai/`
前缀让它查不到，其次才是三个模型确实未收录。注册之后这两类原因都消掉了，
`agent_cost` 与本模块的折算结果严格相等。）

三件此前被忽略、而实测证明会显著改变结论的事：

1. **缓存命中**。DashScope 隐式缓存默认开启且已在生效——冒烟里被测侧
   129262 个输入 token 有 93312 命中缓存（72%）。按全价折算会**高估成本**。
2. **推理 token**。模拟用户实测 4980 个输出 token 里 4451 是 reasoning，
   它们按输出价计费，此前完全没算进去。
3. **判官在另一个服务商**，且 DeepSeek 有峰谷价：同样的调用在高峰时段贵一倍。

价格会变。所有数字都带取价日期，过期就该重新核。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

PRICE_SNAPSHOT_DATE = "2026-09-06"

# 隐式缓存命中按输入价的 20% 计费（阿里云 Context Cache 文档）。
# 文档之间有分歧：价目页写"缓存命中 10%"，Context Cache 文档写
# "显式缓存 10% / 隐式缓存 20%"。我们用的是隐式缓存，故按 20% 计——**取贵的那个**，
# 宁可高估成本也不要在报告里低报。
DASHSCOPE_IMPLICIT_CACHE_RATIO = 0.20

# 曾被列为"折扣未公开"的三个模型，2026-09-06 查到了明确单价，
# 于是成本区间从三倍宽收敛成确定值。留空集是为了保留这个机制：
# 将来再遇到查不到价的模型，加进来就会自动改为输出区间而不是假装精确。
DASHSCOPE_CACHE_RATIO_UNKNOWN: frozenset[str] = frozenset()

# 实测查得的缓存命中单价（元/百万 tok），优先于按比例推算。
# 来源：千问 AI 平台模型页与阿里云百炼价目页，核验 2026-09-06。
DASHSCOPE_CACHE_PRICE_OVERRIDES: dict[str, float] = {
    "qwen3.8-max": 1.5,
    "qwen3.8-flash": 0.1,
    "deepseek-v4-pro-0813": 1.2,
}


class Tier(StrEnum):
    """DeepSeek 的峰谷时段。谷时价格是峰时的一半。"""

    PEAK = "peak"
    OFF_PEAK = "off_peak"


@dataclass(frozen=True)
class Price:
    """元 / 百万 token。

    **`completion_tokens` 已经包含 `reasoning_tokens`**，不要再加一遍。
    2026-09-06 实测 qwen3.6-plus：prompt 32 + completion 2865 = total 2897，
    而 completion_tokens_details.reasoning_tokens = 2709，
    2865 − 2709 = 156 正好是可见回复的长度。DeepSeek 侧同理。
    （注意 `completion_tokens_details.text_tokens` 在实测里等于 completion_tokens 本身，
    并不是"可见部分"，不能拿它做差——用它会把成本算错。）

    `cached_input` 为 None 表示"该模型缓存命中的单价未取得"——
    这时成本只能给出区间，不能给一个假装精确的数字。
    """

    input: float
    output: float
    cached_input: float | None = None

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
        """缓存单价未知时，按全价计——宁可高估，不可低估。"""
        cached = min(cached_tokens, prompt_tokens)
        uncached = prompt_tokens - cached
        cached_rate = self.input if self.cached_input is None else self.cached_input
        return (
            uncached / 1e6 * self.input
            + cached / 1e6 * cached_rate
            + completion_tokens / 1e6 * self.output
        )

    def cost_floor(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int) -> float:
        """缓存单价未知时的下界：假定命中部分按 20% 计。仅用于给出区间。"""
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


# 来源 https://help.aliyun.com/zh/model-studio/model-pricing 北京地域，核验 2026-09-06。
# qwen3.x-max 标注为**非思考模式**单价，思考模式单价未取得。
DASHSCOPE_PRICES: dict[str, Price] = {
    "qwen3.7-max-2026-06-08": _dashscope(12.0, 36.0, "qwen3.7-max-2026-06-08"),
    "qwen3.8-max": _dashscope(12.0, 36.0, "qwen3.8-max"),
    "qwen3.8-flash": _dashscope(0.8, 2.7, "qwen3.8-flash"),
    "deepseek-v4-pro-0813": _dashscope(12.0, 24.0, "deepseek-v4-pro-0813"),
    "glm-5.2": _dashscope(8.0, 28.0, "glm-5.2"),
    # 模拟用户（冻结评测栈）。此前不在快照里，导致它是唯一算不出钱的角色——
    # 而它每 24 题产出约 85k 输出 token、其中 94% 是推理，按输出价计费，不是小钱。
    #
    # 2/12 是**输入 ≤256K 档**的单价。百炼对部分模型按单次请求的输入长度阶梯计价，
    # 但我们的模拟用户输入最大只有 2096 tok，永远落在最低档，所以扁平单价成立。
    # 若将来把它用在长上下文场景，这个价必须重新分档。
    #
    # 缓存：显式创建 2.5（输入价 125%）、显式命中 0.2（10%）；我们用的是隐式缓存，
    # 按 20% 计 = 0.4。但隐式缓存最小长度是 1024 tok，而该角色输入中位数只有 848，
    # 实测 120 次调用仅 43 次够到门槛、且服务端一次都没报命中，
    # 所以这个单价目前从不被触发（全按 input 计，偏保守）。
    "qwen3.6-plus-2026-04-02": Price(input=2.0, output=12.0, cached_input=0.4),
    # 判官模型。**来源可信度低于上面几行**：官方定价页是 JS 动态加载，抓不到，
    # 这三个数字来自二手报道（输入 0.1 / 输出 0.4 / 缓存 0.01 元每百万 token）。
    # 判官成本占比极小（一次全量复判约 0.07 元），即使有偏差也不影响任何选型结论；
    # 但凡要把它写进对外报告，必须先从控制台账单核对一次。
    "qwen3.7-flash-2026-07-15": Price(input=0.1, output=0.4, cached_input=0.01),
}

# 来源 https://api-docs.deepseek.com/zh-cn/quick_start/pricing 核验 2026-09-06。
# 判官走 DeepSeek 官方端点，与 DashScope 上同名模型**不是一套价格**。
DEEPSEEK_PRICES: dict[tuple[str, Tier], Price] = {
    ("deepseek-v4-flash", Tier.PEAK): Price(input=3.0, output=9.0, cached_input=0.10),
    ("deepseek-v4-flash", Tier.OFF_PEAK): Price(input=1.5, output=4.5, cached_input=0.05),
    ("deepseek-v4-pro", Tier.PEAK): Price(input=9.0, output=27.0, cached_input=0.30),
    ("deepseek-v4-pro", Tier.OFF_PEAK): Price(input=4.5, output=13.5, cached_input=0.15),
}

# 智谱开放平台 bigmodel.cn（**国内版**）。核验 2026-09-06。
# 注意与境外的 z.ai 区分：那边按美元计价（GLM-5.3-Flash $0.075/$0.25），
# 端点也不同（api.z.ai vs open.bigmodel.cn），两边不能混用价目表。
# GLM-5.3-Flash 当前有限时五折，下面记的是**原价**——宁可高估，
# 也不要把促销价写进快照后忘了它会到期。
ZHIPU_PRICES: dict[str, Price] = {
    "glm-5.3-flash": Price(input=0.8, output=2.8, cached_input=0.23),
}

# 高峰：北京时间周一至周五 9:00-12:00 与 14:00-18:00（= UTC 01:00-04:00 / 06:00-10:00）。
_PEAK_WINDOWS_BEIJING = ((9, 12), (14, 18))
_BEIJING_OFFSET = timedelta(hours=8)


def deepseek_tier(moment: datetime | None = None) -> Tier:
    """判定某一时刻属于 DeepSeek 的高峰还是空闲时段。"""
    moment = moment or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    beijing = moment.astimezone(UTC) + _BEIJING_OFFSET
    if beijing.weekday() >= 5:  # 周末全天空闲
        return Tier.OFF_PEAK
    return (
        Tier.PEAK
        if any(start <= beijing.hour < end for start, end in _PEAK_WINDOWS_BEIJING)
        else Tier.OFF_PEAK
    )


def price_for(model: str, moment: datetime | None = None) -> Price | None:
    """按模型名取价。DeepSeek 官方端点的价格随时段变化。"""
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
# litellm 价格注册
# ---------------------------------------------------------------------------
#
# 上游用 `completion_cost()` 记账，实测它对我们的每一次调用都抛异常，
# `get_response_cost()` 捕获后 `return 0.0`——这就是 summary.json 里
# `agent_cost_reported_by_upstream` 恒为 0 的原因。
#
# 2026-09-07 核实了真正的原因，与此前文档记载的**不同**：
#
#   litellm 1.82.6 其实收录了 `deepseek/deepseek-v4-pro`（1.32/3.96 美元每百万 tok），
#   但我们下发的模型名带 `openai/` 前缀（走 OpenAI 兼容端点），
#   litellm 按 custom_llm_provider=openai 去查，于是
#   "This model isn't mapped yet. model=deepseek-v4-pro, custom_llm_provider=openai"。
#
#   另外三个（qwen3.7-flash / qwen3.6-plus / glm-5.3-flash）确实完全未收录。
#
# 所以两类原因都要治：按**我们实际下发的键**注册，且四个模型全注册。
#
# ## 货币：注册的是**人民币**，不是美元
#
# litellm 内置价格是美元，Langfuse 面板也会给数字加美元符号。我们仍然注册人民币，
# 理由是**可核对性优先于符号正确**：
#
#   - 人民币是本项目所有成本报告的口径（analyze_eval.py、docs、PR）。注册人民币后，
#     litellm 算出的数与我们自己算的数**严格相等**，可以用测试钉死
#     （见 tests/test_litellm_pricing.py）。
#   - 换算成美元需要引入一个汇率假设。汇率每天都在变，而快照不会自动更新——
#     那是一个会**静默过期**的错误，比一个显式标注过的符号错误危险。
#
# 因此：凡是来自 litellm / Langfuse 的成本数字，单位一律是**元**。
# 字段名与文档必须写明这一点，不能让读的人以为是美元。
LITELLM_COST_CURRENCY = "CNY"

# 我们下发给 litellm 的模型名前缀（OpenAI 兼容端点）。与 evaluation.litellm_model 一致。
LITELLM_PREFIX = "openai/"

# 一条 litellm 成本条目。值要么是单价（float），要么是 provider/mode（str）。
# 不用 object：那会让所有算术都得先 cast，反而掩盖类型错误。
CostEntry = dict[str, float | str]


def _cost_entry(price: Price, model: str) -> CostEntry:
    """把一条价格快照翻成 litellm 的成本条目。**单位是元/token。**"""
    entry: CostEntry = {
        "litellm_provider": "openai",
        "mode": "chat",
        "input_cost_per_token": price.input / 1e6,
        "output_cost_per_token": price.output / 1e6,
    }
    if price.cached_input is not None:
        # 两个字段名都要给：litellm 在不同代码路径上读的不是同一个。
        entry["cache_read_input_token_cost"] = price.cached_input / 1e6
        entry["input_cost_per_token_cache_hit"] = price.cached_input / 1e6
    return entry


def litellm_model_cost(moment: datetime | None = None) -> dict[str, CostEntry]:
    """本快照对应的 litellm 成本表。键是**实际下发的模型名**（带 openai/ 前缀）。

    DeepSeek 的价格随峰谷时段变化，所以这张表是**某一时刻**的快照；
    跨时段的长跑会按注册时的档位记账，与 analyze_eval 按运行时刻折算的结果
    可能有出入——那是 DeepSeek 计价规则本身的粒度，不是这里的缺陷。
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
    """把价格快照注册进 litellm，返回注册了什么（供 manifest 留痕）。

    只在真正要跑评测时调用——litellm 是 bench 组的重依赖，
    不该因为导入 pricing 就被拖进来。
    """
    import litellm

    table = litellm_model_cost(moment)
    litellm.register_model(table)
    return table
