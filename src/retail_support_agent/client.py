"""The UI's client for the API.

The demo UI used to build a `RetailAgent` in its own process under a hard-coded user id,
which meant every authorization check the service performs was skipped by the only path a
visitor could take. It now calls the service instead, carrying the token the visitor
signed in with, so the demo and an API caller are authorized by the same code.

The transport is `urllib`. The call is one JSON POST to a host on the same compose
network, and the alternative was a dependency in the runtime image for that.

Refusals are turned into distinct exceptions rather than one, because they need different
answers from the person reading them: 401 means sign in again, 403 means this account may
not do this, and 502 means the model or a tool failed and the account is not the problem.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Long enough for a model call behind the API, which is the slowest thing on the path.
TIMEOUT_SECONDS = 180.0

# A 502 carries the upstream failure, which can be a provider's whole JSON body. Enough
# of it to identify the failure, not enough to fill a page.
MAX_DETAIL = 300


class ApiError(Exception):
    """The request did not succeed. The message is safe to show a visitor."""


class UnauthenticatedError(ApiError):
    """401. The token is missing, expired or does not verify."""


class ForbiddenError(ApiError):
    """403. The token verified and this account may not do this.

    Includes the case of a token that verifies for a subject with no account here, which
    is what an unprovisioned identity gets.
    """


class UnavailableError(ApiError):
    """The service could not be reached, or failed for a reason that is not the caller."""


@dataclass(frozen=True)
class Reply:
    """One answer from `/chat`, in the shape the UI renders."""

    reply: str
    status: str
    tool_calls: tuple[str, ...] = ()
    pending_confirmation: str | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class Account:
    """Who the service says the caller is, after the database has decided.

    Rendered on the page. It is the visible proof that the retail identity and the roles
    come from `identities` and `role_memberships` rather than from anything the browser
    sent.
    """

    user_id: str
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    auth: str = "disabled"


@dataclass(frozen=True)
class Api:
    """Calls one service. Holds no token: the token is per visitor, and this is shared."""

    base_url: str
    timeout: float = field(default=TIMEOUT_SECONDS)

    def health(self) -> Mapping[str, Any]:
        return self._call("GET", "/health", token=None)

    def me(self, token: str | None) -> Account:
        body = self._call("GET", "/me", token=token)
        return Account(
            user_id=str(body.get("user_id", "")),
            roles=tuple(body.get("roles") or ()),
            permissions=tuple(body.get("permissions") or ()),
            auth=str(body.get("auth", "disabled")),
        )

    def chat(self, token: str | None, session_id: str, message: str) -> Reply:
        body = self._call(
            "POST",
            "/chat",
            token=token,
            payload={"session_id": session_id, "message": message},
        )
        return Reply(
            reply=str(body.get("reply", "")),
            status=str(body.get("status", "")),
            tool_calls=tuple(body.get("tool_calls") or ()),
            pending_confirmation=body.get("pending_confirmation"),
            stop_reason=body.get("stop_reason"),
        )

    def reset(self, token: str | None, session_id: str) -> None:
        quoted = urllib.parse.quote(session_id, safe="")
        self._call("POST", f"/session/{quoted}/reset", token=token)

    def _call(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(dict(payload)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # `from None`: the chained exception holds `request`, and `request.headers`
            # holds the bearer token. A traceback that reached a log would carry it.
            raise _refusal(exc) from None
        except Exception:
            raise UnavailableError("The service is not answering.") from None


def _refusal(exc: urllib.error.HTTPError) -> ApiError:
    """Map a status to the exception whose answer is different."""
    try:
        detail = str(json.loads(exc.read().decode("utf-8")).get("detail", ""))
    except Exception:
        detail = ""
    detail = detail[:MAX_DETAIL]
    if exc.code == 401:
        return UnauthenticatedError(detail or "Your session has expired. Sign in again.")
    if exc.code == 403:
        return ForbiddenError(detail or "This account may not do that.")
    if exc.code == 409:
        return ApiError(detail or "That session is being used by another process.")
    if exc.code == 422:
        return ApiError(detail or "The service rejected that request.")
    return UnavailableError(detail or f"The service failed ({exc.code}).")
