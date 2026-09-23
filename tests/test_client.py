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
    ConflictError,
    ForbiddenError,
    SessionBusyError,
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


class TestTheTwoKindsOfConflict:
    """Six conditions answer 409 and they do not share an answer.

    Five are permanent for the request that hit them - a case no longer open, one
    already closed, an escalation out of time, one already decided - and one, a turn
    already running on the session, clears by itself. Rendered as one state, the page
    told a supervisor whose escalation had expired to wait for it to finish and send
    again, which describes neither what happened nor anything that would help.

    Which one a 409 is cannot be read off the status or matched on the service's
    sentence, which is written for a person. The route knows, so the route says.
    """

    def test_a_busy_session_is_the_conflict_that_clears_by_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failing(monkeypatch, 409, "Session s1 is already handling a message.")
        with pytest.raises(SessionBusyError):
            Api(BASE).chat("a.token.value", "s1", "hello")

    def test_the_streaming_route_says_the_same_thing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The route the page actually uses. `chat` and `chat_stream` are written out
        separately, so a rule proved on one of them is not proved on the other."""
        failing(monkeypatch, 409, "Session s1 is already handling a message.")
        with pytest.raises(SessionBusyError):
            list(Api(BASE).chat_stream("a.token.value", "s1", "hello"))

    def test_an_expired_escalation_is_not_a_busy_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect this class exists for: found by walking a supervisor's page after
        the approval window ran out."""
        failing(monkeypatch, 409, "That request expired and must be raised again.")
        with pytest.raises(ConflictError) as caught:
            Api(BASE).decide("a.token.value", 1, True)
        assert not isinstance(caught.value, SessionBusyError)

    @pytest.mark.parametrize(
        ("call", "detail"),
        [
            (lambda api: api.assign("t", 1, "subject"), "That ticket is not open."),
            (lambda api: api.close("t", 1, "resolved", "done"), "That ticket is already closed."),
            (lambda api: api.work("t", 1), "That ticket is closed."),
            (lambda api: api.decide("t", 1, False), "That request was already decided."),
        ],
    )
    def test_no_case_route_reports_a_busy_session(
        self, monkeypatch: pytest.MonkeyPatch, call: Any, detail: str
    ) -> None:
        failing(monkeypatch, 409, detail)
        with pytest.raises(ConflictError) as caught:
            call(Api(BASE))
        assert not isinstance(caught.value, SessionBusyError)
        assert detail in str(caught.value)


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


class Stream:
    """A stand-in for an open event-stream response: iterable by line, closeable."""

    def __init__(self, *payloads: Any) -> None:
        lines: list[bytes] = []
        for payload in payloads:
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            lines.append(f"data: {raw}\n".encode())
            lines.append(b"\n")
        self._lines = lines

    def __enter__(self) -> Stream:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(self._lines)


def streaming(monkeypatch: pytest.MonkeyPatch, *payloads: Any) -> list[Any]:
    seen: list[Any] = []

    def urlopen(request: Any, **_kwargs: object) -> Stream:
        seen.append(request)
        return Stream(*payloads)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


class TestReadingAStreamedTurn:
    """M7.1. The events are a view of a turn; the last one is the turn's answer."""

    def test_progress_then_one_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        streaming(
            monkeypatch,
            {"kind": "thinking", "tool": None, "detail": ""},
            {"kind": "tool", "tool": "get_order_details", "detail": ""},
            {"kind": "reply", "reply": "Pending.", "status": "answered", "tool_calls": ["x"]},
        )
        events = list(Api(BASE).chat_stream("t", "s1", "hello"))
        assert [e.kind for e in events] == ["thinking", "tool", "reply"]
        assert events[1].tool == "get_order_details"
        assert events[-1].reply is not None
        assert events[-1].reply.reply == "Pending."
        assert events[-1].reply.tool_calls == ("x",)

    def test_only_the_last_event_carries_a_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller that keeps the last one is a caller of `chat`."""
        streaming(
            monkeypatch,
            {"kind": "thinking"},
            {"kind": "reply", "reply": "Hi.", "status": "answered"},
        )
        events = list(Api(BASE).chat_stream(None, "s1", "hello"))
        assert [e.reply is not None for e in events] == [False, True]

    def test_the_token_is_carried_the_same_way(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = streaming(monkeypatch, {"kind": "reply", "reply": "", "status": "answered"})
        list(Api(BASE).chat_stream("a-token", "s1", "hello"))
        assert seen[0].get_header("Authorization") == "Bearer a-token"
        assert seen[0].get_header("Accept") == "text/event-stream"

    @pytest.mark.parametrize(
        ("code", "expected"),
        [(401, UnauthenticatedError), (403, ForbiddenError), (502, UnavailableError)],
    )
    def test_a_refusal_is_raised_before_any_event(
        self, monkeypatch: pytest.MonkeyPatch, code: int, expected: type[ApiError]
    ) -> None:
        """Raised by the call, not by the first `next`. The caller can refresh a token
        and try again without having already started rendering a turn."""
        failing(monkeypatch, code, "no")
        with pytest.raises(expected):
            Api(BASE).chat_stream("t", "s1", "hello")

    def test_an_error_event_becomes_an_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The turn failed after the response began, so the service had no status code
        left to send. It still has to reach the caller as a failure."""
        streaming(
            monkeypatch,
            {"kind": "thinking"},
            {"kind": "error", "detail": "Model or tool call failed: boom"},
        )
        events = Api(BASE).chat_stream("t", "s1", "hello")
        assert next(events).kind == "thinking"
        with pytest.raises(UnavailableError, match="boom"):
            next(events)

    def test_an_unreadable_event_is_a_failure_not_a_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stream is the only account of what happened. Dropping part of it would
        show a turn that is missing a step nobody knows about."""
        streaming(monkeypatch, "{not json")
        with pytest.raises(UnavailableError):
            list(Api(BASE).chat_stream("t", "s1", "hello"))


def test_the_audit_route_is_parsed_into_records() -> None:
    """`detail` is passed through whole rather than flattened: which of its fields
    matter depends on the kind, and the page is a better place to decide that."""
    from retail_support_agent.client import _audit_record

    parsed = _audit_record(
        {
            "kind": "access_denied",
            "detail": {"tool": "get_order_details", "authority": "self", "reason": "no"},
            "operation_id": None,
            "created_at": "2026-09-19T10:00:00+00:00",
        }
    )
    assert parsed.kind == "access_denied"
    assert parsed.detail["tool"] == "get_order_details"
    assert parsed.operation_id is None


def test_an_audit_row_with_missing_fields_does_not_raise() -> None:
    """A row the service adds a field to, or leaves one out of, must not take the page
    down: the trail is what a reviewer reads when something has already gone wrong."""
    from retail_support_agent.client import _audit_record

    parsed = _audit_record({})
    assert parsed.kind == ""
    assert parsed.detail == {}
    assert parsed.created_at == ""
