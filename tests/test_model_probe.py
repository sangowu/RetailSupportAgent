"""探针纯逻辑的单元测试：不发网络请求，可在 CI 离线运行。"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from openai import APIStatusError

from retail_support_agent.config import CANDIDATE_MODELS, REFERENCE_MODEL, Settings
from retail_support_agent.model_probe import (
    _BUDGET_IN_ERROR,
    ProbeOutcome,
    describe_error,
)

DUMMY_KEY = "sk-probe-only-not-a-real-key-0000"


def test_budget_regex_extracts_mapped_budget() -> None:
    message = "max_completion_tokens [512] must be greater than thinking_budget [8192]"
    match = _BUDGET_IN_ERROR.search(message)
    assert match is not None
    assert int(match.group(1)) == 8192


def test_budget_regex_ignores_unrelated_errors() -> None:
    assert _BUDGET_IN_ERROR.search("'reasoning_effort' must be one of: 'low', 'high'") is None


def test_describe_error_keeps_type_and_truncates_message() -> None:
    info = describe_error(ValueError("x" * 2000))
    assert info["type"] == "ValueError"
    assert len(info["message"]) == 800
    assert "http_status" not in info  # 非 API 错误不应伪造状态码


def test_probe_outcome_serializes_to_plain_json() -> None:
    outcome = ProbeOutcome(
        name="basic",
        status="ok",
        request={"model": "m", "messages": []},
        latency_s=0.5,
        findings={"callable": True},
    )
    dumped = json.loads(json.dumps(outcome.to_json(), ensure_ascii=False))
    assert dumped["name"] == "basic"
    assert dumped["findings"]["callable"] is True
    assert dumped["error"] is None


def test_candidate_pool_matches_documented_experiment_design() -> None:
    assert REFERENCE_MODEL in CANDIDATE_MODELS
    assert len(CANDIDATE_MODELS) == 5
    assert len(set(CANDIDATE_MODELS)) == 5, "候选池不能有重复模型"


class TestSecretHandling:
    """密钥绝不能出现在日志、报告或异常里——这是硬性要求，用测试守住。"""

    @pytest.fixture
    def settings(self, monkeypatch: pytest.MonkeyPatch) -> Settings:
        monkeypatch.setenv("DASHSCOPE_API_KEY", DUMMY_KEY)
        return Settings()  # type: ignore[call-arg]

    def test_repr_and_str_do_not_leak_key(self, settings: Settings) -> None:
        assert DUMMY_KEY not in repr(settings)
        assert DUMMY_KEY not in str(settings)

    def test_model_dump_does_not_leak_key(self, settings: Settings) -> None:
        assert DUMMY_KEY not in json.dumps(settings.model_dump(), default=str)

    def test_key_presence_reports_only_presence(self, settings: Settings) -> None:
        presence = settings.key_presence()
        assert DUMMY_KEY not in presence
        # 连末几位都不暴露：只允许出现长度
        assert DUMMY_KEY[-4:] not in presence
        assert str(len(DUMMY_KEY)) in presence

    def test_secret_value_requires_explicit_call(self, settings: Settings) -> None:
        assert settings.dashscope_api_key.get_secret_value() == DUMMY_KEY


def test_api_status_error_records_http_status() -> None:
    class _Response:
        status_code = 400
        headers: ClassVar[dict[str, str]] = {}
        request = None

    error = APIStatusError(
        "bad request",
        response=_Response(),  # type: ignore[arg-type]
        body={"code": "invalid_parameter_error"},
    )
    info = describe_error(error)
    assert info["http_status"] == 400
    assert info["api_code"] == "invalid_parameter_error"
