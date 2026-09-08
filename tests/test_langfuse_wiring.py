"""Langfuse wiring: the wrong callback name does not error, it silently reports nothing.

Measured 2026-09-07 on langfuse 4.15.1 + litellm 1.82.6:

    "langfuse"      -> initialization raises AttributeError (looking for the v2 SDK's
                       langfuse.version); litellm catches it and returns None
    "langfuse_otel" -> returns LangfuseOtelLogger normally

Upstream tau2 uses the former, so flipping tau2.config.USE_LANGFUSE to True is wrong: a
run completes with no trace in the dashboard and no sign anything went wrong.
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

pytest.importorskip("litellm", reason="litellm is in the bench group; skip if absent")

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
        assert "not fully configured" in status["langfuse_reason"]

    def test_half_configured_does_not_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A partial configuration cannot connect and would surface mid-run, so it stays off."""
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
        """Upstream's name gets its own constant so a search leads to why it is unusable."""
        assert LANGFUSE_BROKEN_CALLBACK == "langfuse"
        assert LANGFUSE_CALLBACK != LANGFUSE_BROKEN_CALLBACK

    def test_broken_callback_really_is_broken(self) -> None:
        """Measured, not assumed: the old callback initializes to None on the current SDK.

        This will change as dependencies are upgraded. The day it turns green, litellm
        has fixed v4 support and the choice can be re-evaluated. The change itself is
        the signal, so this is not skipped.
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
        assert broken is None, "the old callback works now; re-evaluate LANGFUSE_CALLBACK"
        assert working is not None


class TestSecretsAndCurrency:
    def test_status_never_contains_the_keys(self, configured: Settings) -> None:
        status = enable_langfuse(configured)
        assert SECRET not in str(status)
        assert PUBLIC not in str(status)
        assert status["langfuse_host"] == HOST

    def test_manifest_says_langfuse_cost_is_not_reported(self, configured: Settings) -> None:
        """A dashboard cost of 0 is an upstream gap, not a free run, and the manifest
        must say so.

        Measured 2026-09-07: litellm 1.82.6's langfuse_otel never sends
        `langfuse.observation.cost_details.*` and observations carry an empty
        costDetails. Letting Langfuse compute from usage fails too: the usage it
        receives has no cache breakdown, and with 95% agent-side cache hits, full-price
        arithmetic overstates cost about 5x.
        """
        status = enable_langfuse(configured)
        assert status["langfuse_cost_reported"] is False
        assert "analyze_eval" in status["langfuse_cost_note"]

    def test_litellm_still_does_not_emit_cost_details(self) -> None:
        """Watches upstream: this turns red the day litellm starts sending cost_details.

        Red is not bad news here; it means the Langfuse cost question can be revisited.
        """
        import pathlib

        import litellm

        root = pathlib.Path(litellm.__file__).parent / "integrations" / "langfuse"
        emitted = any(
            "cost_details" in f.read_text(encoding="utf-8", errors="ignore")
            for f in root.glob("*.py")
        )
        assert not emitted, "litellm now sends cost_details; revisit the Langfuse cost convention"

    def test_currency_is_recorded_in_the_manifest(self, configured: Settings) -> None:
        """The dashboard shows a dollar sign over a CNY value. Without this in the
        manifest, someone will eventually report it as USD."""
        status = enable_langfuse(configured)
        assert status["langfuse_cost_currency"] == LITELLM_COST_CURRENCY == "CNY"

    def test_callback_is_registered_once(self, configured: Settings) -> None:
        import litellm

        enable_langfuse(configured)
        enable_langfuse(configured)
        registered = sum(1 for c in litellm.callbacks if c == LANGFUSE_CALLBACK)
        assert registered == 1


class TestSessionCorrelation:
    """A dashboard trace must map back to the run that produced it, or observation and
    results describe different things.

    Langfuse groups by session. litellm's langfuse_otel reads `session_id` / `tags` only
    from `litellm_params["metadata"]`, which can only be passed per call: there is no
    global entry point, and the one environment variable,
    `LANGFUSE_TRACING_ENVIRONMENT`, is a dev/prod name rather than a session id. Hence
    the wrapper on the shared exit point.
    """

    @pytest.fixture
    def calls(self) -> Iterator[list[dict[str, Any]]]:
        """Replace tau2's shared exit point with a recorder and restore it afterwards."""
        pytest.importorskip("tau2", reason="tau2 is in the bench group; skip if absent")
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
        """Resuming tags a second time, when the wrapper is already installed: only the
        id should change.

        Stacking would not fail immediately, but each run would add a frame, and the
        outer setdefault would keep the inner id from ever being written. The failure
        mode is "looks fine".
        """
        from tau2.utils import llm_utils

        tag_langfuse_session("run-a")
        wrapped_once = llm_utils.completion
        tag_langfuse_session("run-b")
        assert llm_utils.completion is wrapped_once, "re-tagging must not add another wrapper"

        llm_utils.completion(model="openai/x", messages=[])
        assert calls[0]["metadata"]["session_id"] == "run-b"

    def test_upstream_metadata_wins(self, calls: list[dict[str, Any]]) -> None:
        """If upstream ever passes metadata, theirs wins: this fills gaps, never overwrites."""
        from tau2.utils import llm_utils

        tag_langfuse_session("run-a")
        llm_utils.completion(model="openai/x", messages=[], metadata={"session_id": "theirs"})
        assert calls[0]["metadata"]["session_id"] == "theirs"

    def test_metadata_is_a_litellm_param_and_never_reaches_the_provider(self) -> None:
        """`metadata` must be in litellm's own parameter list.

        A key not on that list is forwarded as a model parameter, or dropped when
        `litellm.drop_params=True`. Neither is wanted: the first makes the server return
        400, the second makes the session id disappear silently.
        """
        from litellm.litellm_core_utils.get_litellm_params import get_litellm_params

        params = get_litellm_params(metadata={"session_id": "run-a"})
        assert params["metadata"] == {"session_id": "run-a"}


class TestFlushOnExit:
    """Without an explicit flush after a run, the last tasks' traces are silently lost.

    Measured 2026-09-07 with real calls and a Langfuse API read-back:

        call then exit immediately -> the trace never appears, exit code still 0
        call, wait, then exit      -> the trace arrives
        call then flush_langfuse   -> the trace arrives

    OTEL's TracerProvider would shut down at process exit, but litellm's langfuse_otel
    never installs the provider globally, so that path never runs.
    """

    def test_flush_reports_false_when_nothing_is_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With Langfuse not wired there is nothing to flush; this returns False rather
        than raising, because failing to export observability must not fail a run whose
        results are fine."""
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
        """An export failure is reported as False, never raised into the run."""
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
        """`_in_memory_loggers` is a private upstream name and the only way to reach the
        span processor.

        The day it is renamed this turns red. Otherwise the flush would silently degrade
        to a no-op and traces would start disappearing again, and only this test catches it.
        """
        from litellm.litellm_core_utils.litellm_logging import _in_memory_loggers

        assert isinstance(_in_memory_loggers, list)

    def test_the_global_tracer_provider_is_not_the_flush_handle(self) -> None:
        """Records why the standard approach is not used: litellm does not install the
        provider globally.

        The standard call is `opentelemetry.trace.get_tracer_provider().force_flush()`,
        but that returns a ProxyTracerProvider, which has no force_flush. If litellm ever
        installs it globally this turns red and the standard call can be restored.
        """
        pytest.importorskip(
            "opentelemetry", reason="installed with litellm's OTEL integration; skip if absent"
        )
        from opentelemetry import trace

        assert not hasattr(trace.get_tracer_provider(), "force_flush")
