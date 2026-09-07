"""Langfuse 接入：选错回调名不会报错，只会静默不上报。

2026-09-07 在 langfuse 4.15.1 + litellm 1.82.6 上实测：

    "langfuse"      -> 初始化抛 AttributeError（找 v2 SDK 的 langfuse.version），
                       litellm 捕获后返回 None
    "langfuse_otel" -> 正常返回 LangfuseOtelLogger

上游 tau2 用的正是前者。所以**把 tau2.config.USE_LANGFUSE 翻成 True 是错的**：
跑完一轮面板上一条 trace 都没有，而运行本身完全正常，没有任何迹象。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from retail_support_agent.config import Settings
from retail_support_agent.evaluation import (
    LANGFUSE_BROKEN_CALLBACK,
    LANGFUSE_CALLBACK,
    LANGFUSE_FLUSH_TIMEOUT_MS,
    LANGFUSE_TAG,
    enable_langfuse,
    flush_langfuse,
    tag_langfuse_session,
)
from retail_support_agent.pricing import LITELLM_COST_CURRENCY

pytest.importorskip("litellm", reason="litellm 属 bench 组，未装则跳过")

PUBLIC = "pk-lf-test-0000000000"
SECRET = "sk-lf-test-1111111111"
HOST = "https://langfuse.example.com"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-eval-test-key-000000000000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET)
    monkeypatch.setenv("LANGFUSE_HOST", HOST)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-eval-test-key-000000000000")
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestAllOrNothing:
    def test_missing_config_does_not_enable(self, unconfigured: Settings) -> None:
        status = enable_langfuse(unconfigured)
        assert status["langfuse_enabled"] is False
        assert "未配置" in status["langfuse_reason"]

    def test_half_configured_does_not_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """半套配置连不上，却要等到跑批中途才暴露——宁可不启用。"""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-eval-test-key-000000000000")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PUBLIC)
        for name in ("LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.has_langfuse() is False
        assert enable_langfuse(settings)["langfuse_enabled"] is False


class TestCallbackChoice:
    def test_we_use_the_otel_callback(self) -> None:
        assert LANGFUSE_CALLBACK == "langfuse_otel"

    def test_the_upstream_callback_name_is_recorded_as_broken(self) -> None:
        """上游用的名字单独留一个常量，让人能顺着它搜到"为什么不能用"。"""
        assert LANGFUSE_BROKEN_CALLBACK == "langfuse"
        assert LANGFUSE_CALLBACK != LANGFUSE_BROKEN_CALLBACK

    def test_broken_callback_really_is_broken(self) -> None:
        """不是听说，是实测：旧回调在当前 SDK 上初始化返回 None。

        这条会随依赖升级而变化——它变绿的那天，说明 litellm 修好了对 v4 的支持，
        那时可以重新评估用哪个。**变化本身就是信号**，所以不 skip。
        """
        pytest.importorskip("langfuse")
        from litellm.litellm_core_utils.litellm_logging import (
            _init_custom_logger_compatible_class,
        )

        broken = _init_custom_logger_compatible_class(
            LANGFUSE_BROKEN_CALLBACK,
            internal_usage_cache=None,
            llm_router=None,
        )
        working = _init_custom_logger_compatible_class(
            LANGFUSE_CALLBACK,
            internal_usage_cache=None,
            llm_router=None,
        )
        assert broken is None, "旧回调居然能用了——重新评估 LANGFUSE_CALLBACK 的选择"
        assert working is not None


class TestSecretsAndCurrency:
    def test_status_never_contains_the_keys(self, configured: Settings) -> None:
        status = enable_langfuse(configured)
        assert SECRET not in str(status)
        assert PUBLIC not in str(status)
        assert status["langfuse_host"] == HOST

    def test_manifest_says_langfuse_cost_is_not_reported(self, configured: Settings) -> None:
        """面板成本恒为 0 是**上游缺口**，不是本轮免费——必须在 manifest 里说清。

        2026-09-07 实测：litellm 1.82.6 的 langfuse_otel 从不发送
        `langfuse.observation.cost_details.*`，observation 里 costDetails 是空的。
        让 Langfuse 自己按用量折算也不行：它收到的用量没有缓存明细，
        而被测侧 95% 输入命中缓存，按全价算会高估约 5 倍。
        """
        status = enable_langfuse(configured)
        assert status["langfuse_cost_reported"] is False
        assert "analyze_eval" in status["langfuse_cost_note"]

    def test_litellm_still_does_not_emit_cost_details(self) -> None:
        """这条盯着上游：哪天 litellm 开始发 cost_details，它会变红。

        变红不是坏消息，是提示可以重新评估「Langfuse 要不要显示成本」。
        """
        import pathlib

        import litellm

        root = pathlib.Path(litellm.__file__).parent / "integrations" / "langfuse"
        emitted = any(
            "cost_details" in f.read_text(encoding="utf-8", errors="ignore")
            for f in root.glob("*.py")
        )
        assert not emitted, "litellm 开始发 cost_details 了——重新评估 Langfuse 的成本口径"

    def test_currency_is_recorded_in_the_manifest(self, configured: Settings) -> None:
        """面板显示美元符号，数值却是人民币。不写进 manifest，早晚有人拿它当美元汇报。"""
        status = enable_langfuse(configured)
        assert status["langfuse_cost_currency"] == LITELLM_COST_CURRENCY == "CNY"

    def test_callback_is_registered_once(self, configured: Settings) -> None:
        import litellm

        enable_langfuse(configured)
        enable_langfuse(configured)
        registered = sum(1 for c in litellm.callbacks if c == LANGFUSE_CALLBACK)
        assert registered == 1


class TestSessionCorrelation:
    """面板上的 trace 要能对回是哪一轮跑的——否则观测和结果各说各话。

    Langfuse 的分组维度是 session。litellm 的 langfuse_otel 只从
    `litellm_params["metadata"]` 里读 `session_id` / `tags`，而这只能逐次调用
    传入：没有全局入口，唯一的环境变量 `LANGFUSE_TRACING_ENVIRONMENT` 是
    dev/prod 那种环境名，不是会话号。所以只能在共用出口上包一层。
    """

    @pytest.fixture
    def calls(self) -> Iterator[list[dict[str, Any]]]:
        """把 tau2 的共用出口换成记录器，用例结束后原样放回。"""
        pytest.importorskip("tau2", reason="tau2 属 bench 组，未装则跳过")
        from tau2.utils import llm_utils

        recorded: list[dict[str, Any]] = []

        def record(**kwargs: Any) -> str:
            recorded.append(kwargs)
            return "resp"

        original = llm_utils.completion
        llm_utils.completion = record
        try:
            yield recorded
        finally:
            llm_utils.completion = original

    def test_session_id_is_injected_into_metadata(self, calls: list[dict[str, Any]]) -> None:
        from tau2.utils import llm_utils

        run = "20260907T141354Z_deepseek-v4-pro_dev"
        tag_langfuse_session(run)
        llm_utils.completion(model="openai/x", messages=[])

        assert calls[0]["metadata"] == {"session_id": run, "tags": [LANGFUSE_TAG, run]}

    def test_second_call_swaps_the_id_without_stacking_wrappers(
        self, calls: list[dict[str, Any]]
    ) -> None:
        """补跑会第二次打标记。那时包装已经装好了，只该换号，不该再套一层。

        套两层不会立刻出错，但每多跑一轮就多一层调用栈，且外层的 setdefault
        会让内层的号永远写不进去——错的方向恰好是「看起来没问题」。
        """
        from tau2.utils import llm_utils

        tag_langfuse_session("run-a")
        wrapped_once = llm_utils.completion
        tag_langfuse_session("run-b")
        assert llm_utils.completion is wrapped_once, "重复打标记不该再套一层"

        llm_utils.completion(model="openai/x", messages=[])
        assert calls[0]["metadata"]["session_id"] == "run-b"

    def test_upstream_metadata_wins(self, calls: list[dict[str, Any]]) -> None:
        """万一上游哪天自己开始传 metadata，以它的为准——我们只补缺，不覆盖。"""
        from tau2.utils import llm_utils

        tag_langfuse_session("run-a")
        llm_utils.completion(model="openai/x", messages=[], metadata={"session_id": "theirs"})
        assert calls[0]["metadata"]["session_id"] == "theirs"

    def test_metadata_is_a_litellm_param_and_never_reaches_the_provider(self) -> None:
        """`metadata` 必须在 litellm 自己的参数名单里。

        不在名单里的键会被当成模型参数转发出去（`litellm.drop_params=True` 时
        则被丢掉），两种结局都不是我们要的：前者让服务端报 400，
        后者让会话号静默消失。
        """
        from litellm.litellm_core_utils.get_litellm_params import get_litellm_params

        params = get_litellm_params(metadata={"session_id": "run-a"})
        assert params["metadata"] == {"session_id": "run-a"}


class TestFlushOnExit:
    """跑完不显式推一次，末尾几题的 trace 会静默丢掉。

    2026-09-07 实测（真实调用 + Langfuse API 回查）：

        调用后立刻退出        -> 面板上永远没有这条 trace，退出码仍是 0
        调用后等待再退出      -> trace 到达
        调用后 flush_langfuse -> trace 到达

    OTEL 的 TracerProvider 本来会在进程退出时自己 shutdown，但 litellm 的
    langfuse_otel **没有把 provider 设成全局的**，那条退出路径轮不到它。
    """

    def test_flush_reports_false_when_nothing_is_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没接 Langfuse 时推不出去是正常的，只该返回 False，不该抛错。

        观测推不出去不该让整轮评测失败——那一轮的**结果**是好的。
        """
        import litellm.litellm_core_utils.litellm_logging as logging_mod

        monkeypatch.setattr(logging_mod, "_in_memory_loggers", [])
        assert flush_langfuse(wait_seconds=0) is False

    def test_flush_calls_force_flush_on_the_span_processor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import litellm.litellm_core_utils.litellm_logging as logging_mod

        calls: list[int] = []

        class FakeProcessor:
            def force_flush(self, timeout_millis: int) -> bool:
                calls.append(timeout_millis)
                return True

        class FakeTracer:
            span_processor = FakeProcessor()

        class FakeLogger:
            tracer = FakeTracer()

        monkeypatch.setattr(logging_mod, "_in_memory_loggers", [FakeLogger()])
        assert flush_langfuse(wait_seconds=0) is True
        assert calls == [LANGFUSE_FLUSH_TIMEOUT_MS]

    def test_flush_survives_a_broken_processor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """导出失败要报告成 False，不能把异常抛进评测流程。"""
        import litellm.litellm_core_utils.litellm_logging as logging_mod

        class Exploding:
            def force_flush(self, timeout_millis: int) -> bool:
                raise RuntimeError("exporter down")

        class FakeTracer:
            span_processor = Exploding()

        class FakeLogger:
            tracer = FakeTracer()

        monkeypatch.setattr(logging_mod, "_in_memory_loggers", [FakeLogger()])
        assert flush_langfuse(wait_seconds=0) is False

    def test_the_upstream_private_name_still_exists(self) -> None:
        """`_in_memory_loggers` 是上游私有名字，是唯一能拿到 span_processor 的入口。

        它改名的那天这条会变红——那时 flush 会静默退化成"什么都不推"，
        而 trace 又开始悄悄丢，只有这条测试拦得住。
        """
        from litellm.litellm_core_utils.litellm_logging import _in_memory_loggers

        assert isinstance(_in_memory_loggers, list)

    def test_the_global_tracer_provider_is_not_the_flush_handle(self) -> None:
        """记下"为什么不用标准做法"：litellm 没把 provider 设成全局的。

        标准写法是 `opentelemetry.trace.get_tracer_provider().force_flush()`，
        但那里拿到的是 ProxyTracerProvider，连 force_flush 都没有。
        哪天 litellm 改成设全局了，这条会变红，届时可以换回标准写法。
        """
        pytest.importorskip("opentelemetry", reason="随 litellm 的 OTEL 集成安装，未装则跳过")
        from opentelemetry import trace

        assert not hasattr(trace.get_tracer_provider(), "force_flush")
