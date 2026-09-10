"""Sign-in, and what the page does with a token that has expired.

The UI's identity handling is a few pure decisions wrapped in Gradio callbacks, so the
decisions are tested here and the callbacks are covered by `tests/test_ui.py` building the
page. What matters: the account on screen comes from the service, an expired token is
renewed once and not in a loop, and the page cannot go back to running the agent itself.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

from retail_support_agent import ui
from retail_support_agent.client import Account, ForbiddenError, UnauthenticatedError
from retail_support_agent.cognito import SignInError, Tokens
from retail_support_agent.ui import Visitor, authenticate, cognito_from_env, describe, retrying

CUSTOMER = Account(
    user_id="ivan_santos_6635",
    roles=("customer",),
    permissions=("order:read", "order:write"),
    auth="required",
)


class FakeCognito:
    def __init__(self, tokens: Tokens | Exception, refreshed: Tokens | Exception | None = None):
        self._tokens = tokens
        self._refreshed = refreshed
        self.refreshes = 0

    def sign_in(self, username: str, password: str) -> Tokens:
        if isinstance(self._tokens, Exception):
            raise self._tokens
        return self._tokens

    def refresh(self, refresh_token: str) -> Tokens:
        self.refreshes += 1
        if isinstance(self._refreshed, Exception):
            raise self._refreshed
        assert self._refreshed is not None
        return self._refreshed


class FakeApi:
    def __init__(self, account: Account | Exception) -> None:
        self._account = account
        self.tokens_seen: list[str | None] = []

    def me(self, token: str | None) -> Account:
        self.tokens_seen.append(token)
        if isinstance(self._account, Exception):
            raise self._account
        return self._account


class TestSigningIn:
    def test_the_account_comes_from_the_service(self) -> None:
        """Not from the token, and not from anything the browser sent."""
        api = FakeApi(CUSTOMER)
        visitor = authenticate(
            FakeCognito(Tokens("a.token.value", 3600, "refresh")),  # type: ignore[arg-type]
            api,  # type: ignore[arg-type]
            "ivan",
            "secret",
        )
        assert visitor.account == CUSTOMER
        assert api.tokens_seen == ["a.token.value"]

    def test_a_session_id_is_new_per_sign_in(self) -> None:
        """Two visitors must not land in one conversation, and neither must one visitor
        signing in twice: the second sign-in could be a different account."""
        first = authenticate(
            FakeCognito(Tokens("t", 3600)),  # type: ignore[arg-type]
            FakeApi(CUSTOMER),  # type: ignore[arg-type]
            "ivan",
            "secret",
        )
        second = authenticate(
            FakeCognito(Tokens("t", 3600)),  # type: ignore[arg-type]
            FakeApi(CUSTOMER),  # type: ignore[arg-type]
            "ivan",
            "secret",
        )
        assert first.session_id and first.session_id != second.session_id

    def test_a_refusal_from_the_pool_reaches_the_caller(self) -> None:
        with pytest.raises(SignInError):
            authenticate(
                FakeCognito(SignInError("no")),  # type: ignore[arg-type]
                FakeApi(CUSTOMER),  # type: ignore[arg-type]
                "ivan",
                "wrong",
            )

    def test_an_unprovisioned_account_fails_at_sign_in_not_on_the_first_message(self) -> None:
        """A token that verifies for a subject with no row in `identities` is
        authenticated and authorized to do nothing. Finding that out here means the page
        says so instead of showing an empty conversation that refuses everything.
        """
        with pytest.raises(ForbiddenError):
            authenticate(
                FakeCognito(Tokens("t", 3600)),  # type: ignore[arg-type]
                FakeApi(ForbiddenError("This subject has no account here.")),  # type: ignore[arg-type]
                "nobody",
                "secret",
            )


class TestAnExpiredToken:
    def test_it_is_renewed_once_and_the_call_retried(self) -> None:
        cognito = FakeCognito(Tokens("t", 3600), refreshed=Tokens("newer.token", 3600))
        visitor = Visitor(token="stale", refresh_token="refresh", account=CUSTOMER)
        attempts: list[str | None] = []

        def call(token: str | None) -> str:
            attempts.append(token)
            if token == "stale":
                raise UnauthenticatedError("expired")
            return "answered"

        updated, result = retrying(visitor, cognito, call)  # type: ignore[arg-type]
        assert result == "answered"
        assert updated.token == "newer.token"
        assert attempts == ["stale", "newer.token"]

    def test_a_second_refusal_is_not_retried_again(self) -> None:
        """A revoked refresh token would otherwise turn one expired session into a stream
        of requests, and the visitor would watch a page that never resolves."""
        cognito = FakeCognito(Tokens("t", 3600), refreshed=Tokens("newer.token", 3600))
        visitor = Visitor(token="stale", refresh_token="refresh", account=CUSTOMER)

        def always_refuse(_token: str | None) -> str:
            raise UnauthenticatedError("expired")

        with pytest.raises(UnauthenticatedError):
            retrying(visitor, cognito, always_refuse)  # type: ignore[arg-type]
        assert cognito.refreshes == 1

    def test_a_refresh_the_pool_refuses_signs_the_visitor_out(self) -> None:
        cognito = FakeCognito(Tokens("t", 3600), refreshed=SignInError("revoked"))
        visitor = Visitor(token="stale", refresh_token="refresh", account=CUSTOMER)

        def refuse(_token: str | None) -> str:
            raise UnauthenticatedError("expired")

        with pytest.raises(UnauthenticatedError):
            retrying(visitor, cognito, refuse)  # type: ignore[arg-type]

    def test_without_a_refresh_token_there_is_nothing_to_try(self) -> None:
        visitor = Visitor(token="stale", account=CUSTOMER)

        def refuse(_token: str | None) -> str:
            raise UnauthenticatedError("expired")

        with pytest.raises(UnauthenticatedError):
            retrying(visitor, None, refuse)


class TestWhatThePageSays:
    def test_the_account_line_names_the_identity_and_the_roles(self) -> None:
        """They are the evidence: both were read from the database for the signed-in
        subject, and no field on the page can change either."""
        line = describe(CUSTOMER)
        assert "ivan_santos_6635" in line
        assert "customer" in line

    def test_an_account_with_no_roles_says_so(self) -> None:
        line = describe(Account(user_id="u", roles=(), auth="required"))
        assert "no roles" in line

    def test_the_unauthenticated_mode_is_stated_on_the_page(self) -> None:
        """Nobody should have to guess whether the service in front of them is checking
        anything."""
        line = describe(Account(user_id="ava_moore_2033", roles=("demo",), auth="disabled"))
        assert "not configured" in line


class TestConfiguration:
    def test_no_pool_configured_means_no_sign_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RSA_COGNITO_REGION", raising=False)
        monkeypatch.delenv("RSA_COGNITO_CLIENT_ID", raising=False)
        assert cognito_from_env() is None

    def test_half_a_configuration_is_not_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A region with no client id would otherwise build a client that refuses every
        sign-in, which looks like a broken pool rather than a missing setting."""
        monkeypatch.setenv("RSA_COGNITO_REGION", "eu-west-1")
        monkeypatch.delenv("RSA_COGNITO_CLIENT_ID", raising=False)
        assert cognito_from_env() is None

    def test_both_halves_build_a_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RSA_COGNITO_REGION", "eu-west-1")
        monkeypatch.setenv("RSA_COGNITO_CLIENT_ID", "1example23client45id")
        assert cognito_from_env() is not None

    def test_the_page_builds_with_a_pool_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The sign-in half of the page is only constructed in this mode, so building it
        without a pool would leave those components untested until a deployment."""
        monkeypatch.setenv("RSA_COGNITO_REGION", "eu-west-1")
        monkeypatch.setenv("RSA_COGNITO_CLIENT_ID", "1example23client45id")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        assert ui.build_demo() is not None


class TestTheDemoCannotBypassTheService:
    def test_the_page_does_not_build_an_agent_of_its_own(self) -> None:
        """The defect M6.7 exists to fix.

        While the UI constructed a `RetailAgent` in process, the only path a visitor could
        take was the one path that skipped token verification, session ownership and the
        role table.

        The check is on what the module imports, because the property is "this module
        cannot", and any test that called it would only show that one path did not. On
        imports rather than on the text, so the comment explaining the history does not
        fail the test that enforces it.
        """
        tree = ast.parse(inspect.getsource(ui))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not imported & {
            "retail_support_agent.agent",
            "retail_support_agent.guardrails",
            "retail_support_agent.domain",
            "retail_support_agent.llm",
        }

    def test_the_token_is_not_handed_to_the_browser(self) -> None:
        """`gr.State` lives in this process. A component the page renders would put the
        token in the client, where the page's own JavaScript could read it."""
        source = inspect.getsource(ui.build_demo)
        rendered: list[str] = [
            line for line in source.splitlines() if "gr.Textbox" in line or "gr.Markdown" in line
        ]
        assert not any("token" in line.lower() for line in rendered)

    def test_the_visitor_state_is_not_serialised_into_a_component(self) -> None:
        state: Any = Visitor(token="a.token.value", account=CUSTOMER)
        assert "a.token.value" not in describe(state.account)
