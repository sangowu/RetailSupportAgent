"""Signing in against a user pool, without a user pool.

`Cognito` is a client for two JSON operations, so everything that matters here is testable
by substituting the transport: which flow is requested, what a refusal is turned into, and
that no failure path carries the password back out.

The last one is not decoration. `urllib.request.Request` holds the posted body, and the
body holds the password, so an exception chained from a transport failure would put a
credential in every traceback that reached a log.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from retail_support_agent import cognito as module
from retail_support_agent.cognito import (
    REFUSED,
    Cognito,
    NeedsPermanentPasswordError,
    SignInError,
    SignInUnavailableError,
    Tokens,
)

CLIENT_ID = "1example23client45id"
REGION = "eu-west-1"


class Recorder:
    """A transport that records what it was asked and answers what it was told to."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, url: str, target: str, payload: Any) -> Any:
        self.calls.append((url, target, dict(payload)))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def granted(**overrides: Any) -> dict[str, Any]:
    result = {"AccessToken": "header.payload.signature", "ExpiresIn": 3600}
    result.update(overrides)
    return {"AuthenticationResult": result}


class TestSigningIn:
    def test_the_flow_and_the_client_are_what_the_pool_is_asked_for(self) -> None:
        recorder = Recorder(granted(RefreshToken="refresh-value"))
        tokens = Cognito(REGION, CLIENT_ID, poster=recorder).sign_in("ava", "secret")

        url, target, payload = recorder.calls[0]
        assert url == f"https://cognito-idp.{REGION}.amazonaws.com/"
        assert target == "InitiateAuth"
        assert payload["AuthFlow"] == "USER_PASSWORD_AUTH"
        assert payload["ClientId"] == CLIENT_ID
        assert tokens.access_token == "header.payload.signature"
        assert tokens.refresh_token == "refresh-value"

    def test_a_pool_that_returns_no_refresh_token_is_not_an_error(self) -> None:
        """The pool decides. Without one, an expired session goes back to the form."""
        tokens = Cognito(REGION, CLIENT_ID, poster=Recorder(granted())).sign_in("ava", "secret")
        assert tokens.refresh_token is None

    def test_a_response_with_no_access_token_is_refused_rather_than_returned(self) -> None:
        """An empty bearer token would become a 401 several layers from the cause."""
        poster = Recorder({"AuthenticationResult": {"ExpiresIn": 3600}})
        with pytest.raises(SignInUnavailableError):
            Cognito(REGION, CLIENT_ID, poster=poster).sign_in("ava", "secret")

    def test_blank_credentials_are_refused_without_asking_the_pool(self) -> None:
        """A form submitted empty is not a sign-in attempt worth a request."""
        recorder = Recorder(granted())
        with pytest.raises(SignInError):
            Cognito(REGION, CLIENT_ID, poster=recorder).sign_in("", "")
        assert recorder.calls == []


class TestWhatARefusalSays:
    """One message for every refusal that could distinguish a real account from an absent
    one. Otherwise the form is an oracle for which usernames exist."""

    @pytest.mark.parametrize(
        "code",
        ("NotAuthorizedException", "UserNotFoundException", "InvalidParameterException", ""),
    )
    def test_every_refusal_that_could_enumerate_gives_the_same_message(self, code: str) -> None:
        assert str(module._refusal(code)) == REFUSED

    def test_rate_limiting_says_so_because_the_answer_is_different(self) -> None:
        """Waiting helps. Trying another password does not, and would make it worse."""
        error = module._refusal("TooManyRequestsException")
        assert not isinstance(error, SignInUnavailableError)
        assert "wait" in str(error).lower()

    @pytest.mark.parametrize("code", ("InternalErrorException", "ServiceUnavailableException"))
    def test_the_pool_failing_is_not_the_visitor_failing(self, code: str) -> None:
        assert isinstance(module._refusal(code), SignInUnavailableError)


class TestChallenges:
    def test_an_account_still_holding_its_temporary_password_is_named(self) -> None:
        """Reaching this means the password was right, so naming it enumerates nothing.

        It is the state every Terraform-created user is in until the operator runs
        `admin-set-user-password --permanent`, and reporting it as a wrong password would
        send whoever hits it to the wrong place entirely.
        """
        poster = Recorder({"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "opaque"})
        with pytest.raises(NeedsPermanentPasswordError):
            Cognito(REGION, CLIENT_ID, poster=poster).sign_in("ava", "temporary")

    def test_a_challenge_this_client_cannot_answer_is_refused(self) -> None:
        poster = Recorder({"ChallengeName": "SMS_MFA", "Session": "opaque"})
        with pytest.raises(SignInError):
            Cognito(REGION, CLIENT_ID, poster=poster).sign_in("ava", "secret")


class TestRefreshing:
    def test_it_asks_for_the_refresh_flow(self) -> None:
        recorder = Recorder(granted(AccessToken="a-newer-token"))
        tokens = Cognito(REGION, CLIENT_ID, poster=recorder).refresh("refresh-value")

        _url, _target, payload = recorder.calls[0]
        assert payload["AuthFlow"] == "REFRESH_TOKEN_AUTH"
        assert payload["AuthParameters"]["REFRESH_TOKEN"] == "refresh-value"
        assert tokens.access_token == "a-newer-token"

    def test_an_empty_refresh_token_is_refused_without_asking(self) -> None:
        recorder = Recorder(granted())
        with pytest.raises(SignInError):
            Cognito(REGION, CLIENT_ID, poster=recorder).refresh("")
        assert recorder.calls == []


class TestConfiguration:
    @pytest.mark.parametrize("region", ("eu-west-1/../evil.example", "EU-WEST-1", ""))
    def test_a_region_that_is_not_one_is_refused(self, region: str) -> None:
        """Configuration reaches the URL. A value that is not a region could point the
        sign-in at another host, and this is the only thing standing between them."""
        with pytest.raises(ValueError):
            Cognito(region, CLIENT_ID)

    def test_an_empty_client_id_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Cognito(REGION, "")


class TestNothingCarriesTheCredentialOut:
    def test_the_token_is_not_in_the_repr(self) -> None:
        """A dataclass repr in a traceback or a debugger would print a live token."""
        text = repr(Tokens(access_token="header.payload.signature", expires_in=3600))
        assert "header.payload.signature" not in text

    def test_a_transport_failure_chains_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The chained exception would hold the Request, and the Request holds the body.

        Checked on the real `post`, because this property belongs to the transport and
        would be lost by anyone rewriting it who did not know why `from None` was there.
        """

        def refuse(*_args: object, **_kwargs: object) -> Any:
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        with pytest.raises(SignInUnavailableError) as caught:
            module.post("https://cognito-idp.eu-west-1.amazonaws.com/", "InitiateAuth", {})
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__

    def test_a_refusal_is_read_from_the_body_and_chains_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = json.dumps({"__type": "NotAuthorizedException", "message": "Incorrect"}).encode()

        def refuse(*_args: object, **_kwargs: object) -> Any:
            raise urllib.error.HTTPError(
                "https://cognito-idp.eu-west-1.amazonaws.com/",
                400,
                "Bad Request",
                {},  # type: ignore[arg-type]
                io.BytesIO(body),
            )

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        with pytest.raises(SignInError) as caught:
            module.post("https://cognito-idp.eu-west-1.amazonaws.com/", "InitiateAuth", {})
        assert str(caught.value) == REFUSED
        assert caught.value.__cause__ is None
