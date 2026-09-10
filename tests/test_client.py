"""The UI's client for the API.

What matters here is the translation, not the transport: a status becomes an exception
whose answer is different, and the token reaches the request without reaching a traceback.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from retail_support_agent import client as module
from retail_support_agent.client import (
    Api,
    ApiError,
    ForbiddenError,
    UnauthenticatedError,
    UnavailableError,
)

BASE = "http://api.internal:8000"


class Answer:
    """A stand-in for one urlopen call, used as a context manager the way urlopen is."""

    def __init__(self, body: Any) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def answering(monkeypatch: pytest.MonkeyPatch, body: Any) -> list[Any]:
    """Capture the requests the client makes and answer each with `body`."""
    seen: list[Any] = []

    def urlopen(request: Any, **_kwargs: object) -> Answer:
        seen.append(request)
        return Answer(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def failing(monkeypatch: pytest.MonkeyPatch, code: int, detail: str = "") -> None:
    payload = json.dumps({"detail": detail}).encode("utf-8")

    def urlopen(*_args: object, **_kwargs: object) -> Any:
        raise urllib.error.HTTPError(BASE, code, "refused", {}, io.BytesIO(payload))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


class TestCarryingTheToken:
    def test_the_token_is_sent_as_a_bearer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = answering(monkeypatch, {"user_id": "ava_moore_2033", "auth": "required"})
        Api(BASE).me("a.token.value")
        assert seen[0].get_header("Authorization") == "Bearer a.token.value"

    def test_no_token_means_no_header_rather_than_an_empty_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`Bearer ` with nothing after it is a malformed credential, and the service
        would answer 401 where the intended question was "what am I without one"."""
        seen = answering(monkeypatch, {"user_id": "ava_moore_2033", "auth": "disabled"})
        Api(BASE).me(None)
        assert seen[0].get_header("Authorization") is None

    def test_a_failure_chains_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The chained exception holds the Request, and its headers hold the token."""
        failing(monkeypatch, 403, "This account does not have order:write.")
        with pytest.raises(ForbiddenError) as caught:
            Api(BASE).chat("a.token.value", "s1", "hello")
        assert caught.value.__cause__ is None
        assert "a.token.value" not in str(caught.value)


class TestWhatEachRefusalBecomes:
    def test_401_is_a_reason_to_sign_in_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failing(monkeypatch, 401, "This service requires a bearer token.")
        with pytest.raises(UnauthenticatedError):
            Api(BASE).me(None)

    def test_403_is_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Signing in again with the same account changes nothing, so it must not be
        confused with an expired token."""
        failing(monkeypatch, 403, "This subject has no account here.")
        with pytest.raises(ForbiddenError) as caught:
            Api(BASE).me("a.token.value")
        assert "no account here" in str(caught.value)

    def test_502_is_the_service_and_not_the_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failing(monkeypatch, 502, "Model or tool call failed: upstream timeout")
        with pytest.raises(UnavailableError):
            Api(BASE).chat("a.token.value", "s1", "hello")

    def test_409_keeps_its_own_meaning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A claim held by another process is not an authorization failure."""
        failing(monkeypatch, 409, "That session is claimed elsewhere.")
        with pytest.raises(ApiError) as caught:
            Api(BASE).chat("a.token.value", "s1", "hello")
        assert not isinstance(caught.value, (UnauthenticatedError, ForbiddenError))

    def test_an_unreachable_service_is_not_a_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*_args: object, **_kwargs: object) -> Any:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        with pytest.raises(UnavailableError):
            Api(BASE).health()

    def test_a_long_upstream_body_is_cut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A provider's whole JSON body would otherwise become the page's status line."""
        failing(monkeypatch, 502, "x" * 5000)
        with pytest.raises(UnavailableError) as caught:
            Api(BASE).chat("a.token.value", "s1", "hello")
        assert len(str(caught.value)) <= module.MAX_DETAIL

    def test_a_refusal_with_no_body_still_says_something(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failing(monkeypatch, 401, "")
        with pytest.raises(UnauthenticatedError) as caught:
            Api(BASE).me(None)
        assert str(caught.value)


class TestReadingTheAnswers:
    def test_a_reply_carries_the_three_states_the_page_shows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answering(
            monkeypatch,
            {
                "reply": "I need you to confirm.",
                "status": "needs_confirmation",
                "tool_calls": ["cancel_pending_order"],
                "pending_confirmation": "cancel #W123",
                "stop_reason": None,
            },
        )
        reply = Api(BASE).chat("a.token.value", "s1", "cancel it")
        assert reply.status == "needs_confirmation"
        assert reply.tool_calls == ("cancel_pending_order",)
        assert reply.pending_confirmation == "cancel #W123"

    def test_an_account_carries_what_the_database_granted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answering(
            monkeypatch,
            {
                "user_id": "ivan_santos_6635",
                "roles": ["customer"],
                "permissions": ["order:read", "order:write"],
                "auth": "required",
            },
        )
        account = Api(BASE).me("a.token.value")
        assert account.user_id == "ivan_santos_6635"
        assert account.roles == ("customer",)
        assert account.auth == "required"

    def test_a_session_id_is_escaped_into_the_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A session id is a string the caller chose. Unescaped, one containing a slash
        would address a different route entirely."""
        seen = answering(monkeypatch, {})
        Api(BASE).reset("a.token.value", "a/b")
        assert seen[0].full_url.endswith("/session/a%2Fb/reset")
