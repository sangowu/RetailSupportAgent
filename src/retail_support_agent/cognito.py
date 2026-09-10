"""Sign in against a Cognito user pool, without an AWS SDK.

`InitiateAuth` and `REFRESH_TOKEN_AUTH` are among the few Cognito operations that carry
no request signature: they are what a client calls precisely because it has no
credentials yet. So this is a plain HTTPS POST, and the runtime image does not gain an
AWS SDK for one endpoint.

Three rules carry this module:

1. **Nothing here is logged, and no exception is chained.** `urllib.request.Request`
   holds the posted body, and the body holds the password. An exception chained from a
   urllib failure carries that object into every traceback, so every raise is
   `from None`.
2. **A failed sign-in reports one message whatever its cause.** "No such user" and
   "wrong password" told apart are an account enumeration oracle, and this pool's
   usernames are short.
3. **This module issues no authorization.** It returns a token. What the holder may do
   is decided by `auth.py` against the database, and this module never inspects a claim.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# The pool answers quickly or not at all. A sign-in is in front of a person waiting on a
# form, so this is shorter than the JWKS fetch, which happens once per key rotation.
TIMEOUT_SECONDS = 10.0

_SERVICE = "AWSCognitoIdentityProviderService"

# Configuration reaches the URL, so it is constrained rather than trusted. A region is
# lowercase letters, digits and hyphens; anything else would let a misconfiguration
# point the sign-in at another host.
_REGION = re.compile(r"^[a-z0-9-]{1,32}$")

# What a person is told when the pool refuses. One message for every refusal that could
# distinguish a real account from an absent one.
REFUSED = "That username and password did not match."


class SignInError(Exception):
    """Sign-in did not produce a token. The message is safe to show a visitor."""


class SignInUnavailableError(SignInError):
    """The pool could not be reached or did not answer.

    Separate from a refusal because the answer is different: a refusal means try other
    credentials, this means try again.
    """


class NeedsPermanentPasswordError(SignInError):
    """The account still holds the temporary password Cognito generated.

    Reaching this requires having sent the correct temporary password, so naming the
    cause enumerates nothing. It is the state a Terraform-created user is in until the
    operator runs `admin-set-user-password --permanent`.
    """


@dataclass(frozen=True)
class Tokens:
    """What a successful sign-in returns. Never logged, never rendered, never stored
    anywhere but the signing-in browser session's server-side state."""

    access_token: str
    expires_in: int
    refresh_token: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial, but the point is real
        """Redacted. A dataclass repr in a traceback or a debugger would otherwise print
        a live bearer token."""
        return f"Tokens(expires_in={self.expires_in}, refresh_token=<redacted>)"


class Poster(Protocol):
    """One JSON call to the Cognito endpoint. Injected so tests need no network."""

    def __call__(self, url: str, target: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


def post(url: str, target: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """POST one Cognito operation and return its parsed body.

    A refusal arrives as HTTP 400 with a `__type` naming the exception. That name is
    translated by the caller; it is never shown to a visitor and never logged.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload)).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"{_SERVICE}.{target}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body: Mapping[str, Any] = json.loads(response.read().decode("utf-8"))
            return body
    except urllib.error.HTTPError as exc:
        # Read the body before the handle closes; `from None` because the chained
        # exception holds `request`, and `request.data` is the password.
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            code = str(detail.get("__type", "")).rsplit("#", 1)[-1]
        except Exception:
            code = ""
        raise _refusal(code) from None
    except Exception:
        raise SignInUnavailableError("The sign-in service could not be reached.") from None


def _refusal(code: str) -> SignInError:
    """Translate a Cognito exception name into what the visitor is told.

    Only `TooManyRequestsException` gets its own message. Every other refusal collapses
    into one: `UserNotFoundException` and `NotAuthorizedException` told apart would say
    which usernames exist.
    """
    if code in ("TooManyRequestsException", "LimitExceededException"):
        return SignInError("Too many attempts. Wait a moment and try again.")
    if code in ("InternalErrorException", "ServiceUnavailableException"):
        return SignInUnavailableError("The sign-in service is not answering.")
    return SignInError(REFUSED)


class Cognito:
    """Sign-in against one user pool app client.

    The client is created with no secret, so nothing here holds a credential of its own:
    the only secret that passes through is the visitor's password, on its way out.
    """

    def __init__(self, region: str, client_id: str, *, poster: Poster = post) -> None:
        if not _REGION.match(region):
            raise ValueError(f"Not a region: {region!r}.")
        if not client_id:
            raise ValueError("An app client id is required.")
        self._url = f"https://cognito-idp.{region}.amazonaws.com/"
        self._client_id = client_id
        self._post = poster

    def sign_in(self, username: str, password: str) -> Tokens:
        """Exchange a username and password for tokens, or raise `SignInError`."""
        if not username or not password:
            raise SignInError(REFUSED)
        body = self._post(
            self._url,
            "InitiateAuth",
            {
                "AuthFlow": "USER_PASSWORD_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {"USERNAME": username, "PASSWORD": password},
            },
        )
        challenge = body.get("ChallengeName")
        if challenge == "NEW_PASSWORD_REQUIRED":
            raise NeedsPermanentPasswordError(
                "This account has not been given a permanent password yet."
            )
        if challenge:
            # MFA and the rest are not configured on this pool. If one ever appears, a
            # refusal is the correct answer from a client that cannot answer it.
            raise SignInError("This account needs a sign-in step this demo cannot perform.")
        return _tokens(body)

    def refresh(self, refresh_token: str) -> Tokens:
        """Exchange a refresh token for a new access token.

        The response carries no new refresh token: the original stays valid until the
        pool's refresh validity expires, and the caller keeps holding it.
        """
        if not refresh_token:
            raise SignInError(REFUSED)
        body = self._post(
            self._url,
            "InitiateAuth",
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {"REFRESH_TOKEN": refresh_token},
            },
        )
        return _tokens(body)


def _tokens(body: Mapping[str, Any]) -> Tokens:
    """Read the authentication result, or fail rather than return a blank token.

    A missing `AccessToken` with a 200 status would otherwise become an empty bearer
    token and a 401 several layers away from the cause.
    """
    result = body.get("AuthenticationResult") or {}
    access = result.get("AccessToken")
    if not isinstance(access, str) or not access:
        raise SignInUnavailableError("The sign-in service returned no token.")
    refresh = result.get("RefreshToken")
    return Tokens(
        access_token=access,
        expires_in=int(result.get("ExpiresIn") or 0),
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
    )
