"""API-layer tests: no model calls, no upstream data.

Sessions are created lazily, and only a real /chat builds a domain environment, so
health checks and input validation can be verified offline without τ²-bench data or
API quota.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from retail_support_agent.agent import RetailAgent, TurnEvent, TurnResult
from retail_support_agent.api import (
    DEMO_USER_ID,
    ChatResponse,
    _as_payload,
    _sse,
    create_app,
)
from retail_support_agent.config import Settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    return TestClient(create_app(settings=Settings()))  # type: ignore[call-arg]


def test_health_reports_milestone_and_demo_identity(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # The demo identity is exposed explicitly so nobody mistakes this for a real user
    # system.
    assert body["demo_user_id"] == DEMO_USER_ID


def test_me_reports_the_demo_identity_and_says_authentication_is_off(
    client: TestClient,
) -> None:
    """With no issuer configured the service keeps its fixed identity, and the page that
    renders this says so rather than looking like a signed-in session."""
    body = client.get("/me").json()
    assert body["user_id"] == DEMO_USER_ID
    assert body["auth"] == "disabled"
    assert body["roles"] == ["demo"]


def test_health_reports_which_image_is_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback is only checkable if the running version can be read from the running
    thing. Nothing pinned this field, and a revert quietly dropped it during M6.8: the
    demo answered, the version was simply unknowable."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    monkeypatch.setenv("RSA_IMAGE", "registry.example/rsa-app:m6.8")
    with TestClient(create_app(settings=Settings())) as client:  # type: ignore[call-arg]
        assert client.get("/health").json()["image"] == "registry.example/rsa-app:m6.8"


def test_health_says_unknown_rather_than_omitting_the_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside a container there is no image. A missing key would make a caller comparing
    versions crash instead of saying it cannot tell."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    monkeypatch.delenv("RSA_IMAGE", raising=False)
    with TestClient(create_app(settings=Settings(_env_file=None))) as client:  # type: ignore[call-arg]
        assert client.get("/health").json()["image"] == "unknown"


def test_empty_message_is_rejected_before_reaching_the_model(client: TestClient) -> None:
    response = client.post("/chat", json={"session_id": "s1", "message": ""})
    assert response.status_code == 422


def test_oversized_message_is_rejected(client: TestClient) -> None:
    response = client.post("/chat", json={"session_id": "s1", "message": "x" * 5000})
    assert response.status_code == 422


def test_missing_session_id_is_rejected(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 422


def test_reset_is_idempotent_for_unknown_session(client: TestClient) -> None:
    response = client.post("/session/never-existed/reset")
    assert response.status_code == 200
    assert response.json()["status"] == "reset"


def test_demo_identity_is_a_simulated_user() -> None:
    # τ²-bench simulated ids look like <name>_<digits> and are not real accounts.
    assert DEMO_USER_ID.endswith("_2033")


class TestResetAllSessions:
    """The reset endpoint `scripts/reset-demo.ps1` depends on.

    Each session holds its own in-memory database copy, so resetting the demo means
    dropping sessions. Evaluation results, baselines and .env on disk are untouched.
    """

    def test_reset_all_reports_how_many_were_cleared(self, client: TestClient) -> None:
        """Zero and N are both success but different events for an operator."""
        assert client.post("/sessions/reset").json()["cleared"] == 0

        # Rejected input cannot create a session, so asking the registry directly would
        # be roundabout; the idempotence of /session/<id>/reset establishes the "no
        # sessions" starting state instead.
        assert client.post("/session/s1/reset").status_code == 200
        assert client.post("/sessions/reset").json()["cleared"] == 0

    def test_reset_all_declares_its_scope(self, client: TestClient) -> None:
        """The scope is in the response so an operator can confirm nothing else was
        deleted."""
        body = client.post("/sessions/reset").json()
        assert body["status"] == "reset"
        assert body["scope"] == "all_sessions"

    def test_health_exposes_active_session_count(self, client: TestClient) -> None:
        """The stop script uses this to tell whether stopping now interrupts a demo."""
        assert client.get("/health").json()["active_sessions"] == 0


class TestTheStreamingRoute:
    """The wire between a turn and a page (M7.1).

    The agent's own behaviour is covered in test_streaming.py; what is here is the shape
    of what goes over the wire, because a page reads that and nothing else.
    """

    def test_an_event_is_one_data_line_and_a_blank_one(self) -> None:
        """A newline inside the data field would end the event early, so the JSON is
        written compact rather than pretty."""
        line = _sse({"kind": "thinking", "tool": None, "detail": ""})
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        assert line.count("\n") == 2

    def test_a_progress_event_carries_no_result(self) -> None:
        payload = _as_payload(TurnEvent(kind="tool", tool="get_order_details"))
        assert payload == {"kind": "tool", "tool": "get_order_details", "detail": ""}

    def test_the_final_event_is_what_chat_answers(self) -> None:
        """A client can read one branch and be a client of either route, which is what
        makes the events a view of a turn rather than a second account of it."""
        result = TurnResult(
            reply="Order #W001 is pending.",
            status="answered",
            tool_calls=["get_order_details"],
        )
        payload = _as_payload(TurnEvent(kind="reply", result=result))
        assert payload == {
            "kind": "reply",
            "reply": "Order #W001 is pending.",
            "status": "answered",
            "tool_calls": ["get_order_details"],
            "pending_confirmation": None,
            "stop_reason": None,
            "ticket_id": None,
        }
        assert set(payload) - {"kind"} <= set(ChatResponse.model_fields)

    def test_the_route_sends_every_event_in_order(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake(self: object, message: str) -> Iterator[TurnEvent]:
            yield TurnEvent(kind="thinking")
            yield TurnEvent(kind="tool", tool="get_order_details")
            yield TurnEvent(
                kind="reply",
                result=TurnResult(reply=f"heard {message}", status="answered"),
            )

        monkeypatch.setattr(RetailAgent, "stream", fake)
        with client.stream(
            "POST", "/chat/stream", json={"session_id": "s-stream", "message": "hello"}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = [
                json.loads(line[len("data:") :])
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert [e["kind"] for e in events] == ["thinking", "tool", "reply"]
        assert events[-1]["reply"] == "heard hello"

    def test_a_failure_mid_turn_becomes_an_event(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no status code left to send: the response began with the first event.
        The failure has to arrive in the shape the client already parses."""

        def fake(self: object, message: str) -> Iterator[TurnEvent]:
            yield TurnEvent(kind="thinking")
            raise RuntimeError("the provider hung up")

        monkeypatch.setattr(RetailAgent, "stream", fake)
        with client.stream(
            "POST", "/chat/stream", json={"session_id": "s-fail", "message": "hello"}
        ) as response:
            assert response.status_code == 200
            events = [
                json.loads(line[len("data:") :])
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert [e["kind"] for e in events] == ["thinking", "error"]
        assert "the provider hung up" in events[-1]["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            {"session_id": "s1", "message": ""},
            {"session_id": "s1", "message": "x" * 5000},
            {"message": "hello"},
        ],
    )
    def test_it_validates_its_input_like_chat_does(
        self, client: TestClient, body: dict[str, str]
    ) -> None:
        assert client.post("/chat/stream", json=body).status_code == 422


class TestTheCaseIdInAnAnswer:
    """M7.2 found the ticket number living only inside the service's prose, and left it
    there rather than have the page dig it out with a regular expression. This is the
    field that makes that unnecessary (M7.3)."""

    def test_an_ordinary_answer_carries_no_case(self, client: TestClient) -> None:
        assert "ticket_id" in ChatResponse.model_fields
        assert ChatResponse(reply="hi", status="answered").ticket_id is None

    @pytest.mark.parametrize("status", ("answered", "needs_confirmation", "denied", "stopped"))
    def test_no_lookup_happens_for_a_status_that_cannot_have_one(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        """Every other turn would otherwise pay for a query whose answer is None."""

        def fake(self: object, message: str) -> Iterator[TurnEvent]:
            yield TurnEvent(kind="reply", result=TurnResult(reply="x", status=status))  # type: ignore[arg-type]

        monkeypatch.setattr(RetailAgent, "stream", fake)
        with client.stream(
            "POST", "/chat/stream", json={"session_id": "s-case", "message": "hello"}
        ) as response:
            events = [
                json.loads(line[len("data:") :])
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        assert events[-1]["ticket_id"] is None
