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
from collections.abc import Iterator, Mapping
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


class ConflictError(ApiError):
    """409. This session is being driven from somewhere else (M2).

    Its own class since M7.2, because it is the one failure a new session resolves, and a
    page that cannot tell it from a malformed request cannot say so.
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
    # The case this session is behind, when it is behind one (M7.3).
    ticket_id: int | None = None


@dataclass(frozen=True)
class Event:
    """One step of a turn, from `/chat/stream` (M7.1).

    `reply` is set on exactly one event, the last, and is what `chat` would have
    returned. Every other event is progress: the model was asked, a tool ran, the guard
    held or refused a write.
    """

    kind: str
    tool: str | None = None
    detail: str = ""
    reply: Reply | None = None


@dataclass(frozen=True)
class Ticket:
    """One case, as the service reports it (M7.3)."""

    id: int
    session_id: str
    user_id: str
    state: str
    summary: str
    requested_by: str = ""
    assignee: str | None = None
    assignee_user_id: str | None = None
    resolution: str | None = None
    created_at: str = ""
    closed_at: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True)
class Approval:
    """One escalated write waiting for a decision."""

    id: int
    session_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    requested_by: str = ""
    ticket_id: int | None = None
    expires_at: str = ""


@dataclass(frozen=True)
class AuditRecord:
    """One line of the audit trail.

    `kind` is the vocabulary the backend writes: `write_executed` for an action that
    ran, `access_denied` for one that was refused, `output_withheld` for an answer
    altered on the way out. `detail` is passed through whole rather than flattened here,
    because which of its fields matter depends on the kind and the page is a better
    place to decide that than the transport.
    """

    kind: str
    detail: Mapping[str, Any]
    operation_id: int | None = None
    created_at: str = ""


@dataclass(frozen=True)
class AgentSession:
    """The session in which an assignee works a case.

    Their own, not the customer's: the customer's conversation belongs to the customer,
    and M3's ownership check enforces that against the person helping them too.
    """

    session_id: str
    ticket_id: int
    user_id: str


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
    # Answered by `/assignable` and never by `/me`: naming somebody else is the one action
    # that needs a subject, and a page that never sees its own cannot misplace it.
    subject: str | None = None


@dataclass(frozen=True)
class Api:
    """Calls one service. Holds no token: the token is per visitor, and this is shared."""

    base_url: str
    timeout: float = field(default=TIMEOUT_SECONDS)

    def health(self) -> Mapping[str, Any]:
        return self._call("GET", "/health", token=None)

    def me(self, token: str | None) -> Account:
        return _account(self._call("GET", "/me", token=token))

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
            ticket_id=body.get("ticket_id"),
        )

    def chat_stream(self, token: str | None, session_id: str, message: str) -> Iterator[Event]:
        """The same turn as `chat`, one step at a time (M7.1).

        Yields progress events and then exactly one `Event` whose `reply` is set, which
        carries what `chat` would have returned. A caller that keeps only the last one is
        a caller of `chat`.

        The refusals are the same exceptions, raised before the first event: the response
        status arrives before the body, so a caller who may not do this never sees a
        stream begin.
        """
        request = self._request(
            "POST",
            "/chat/stream",
            token=token,
            payload={"session_id": session_id, "message": message},
        )
        request.add_header("Accept", "text/event-stream")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # `from None`: the chained exception holds the request, whose headers hold
            # the bearer token.
            raise _refusal(exc) from None
        except Exception:
            raise UnavailableError("The service is not answering.") from None
        return _events(response)

    def reset(self, token: str | None, session_id: str) -> None:
        quoted = urllib.parse.quote(session_id, safe="")
        self._call("POST", f"/session/{quoted}/reset", token=token)

    # ---- cases and escalations (M7.3) -----------------------------------
    #
    # Seven routes M4 built and nothing could reach. The narrowing is the service's: a
    # supervisor's `/tickets` answers with every case and an agent's with their own, and
    # this client does not filter, because "an agent cannot widen their own visibility"
    # is a rule and a client-side filter is a preference.

    def tickets(self, token: str | None, open_only: bool = False) -> tuple[Ticket, ...]:
        body = self._call_list(
            "GET",
            f"/tickets?open_only={'true' if open_only else 'false'}",
            token=token,
        )
        return tuple(_ticket(row) for row in body)

    def assignable(self, token: str | None) -> tuple[Account, ...]:
        body = self._call_list("GET", "/assignable", token=token)
        return tuple(_account(row) for row in body)

    def assign(self, token: str | None, ticket_id: int, assignee: str) -> Ticket:
        return _ticket(
            self._call(
                "POST",
                f"/tickets/{ticket_id}/assign",
                token=token,
                payload={"assignee": assignee},
            )
        )

    def close(self, token: str | None, ticket_id: int, state: str, resolution: str) -> Ticket:
        return _ticket(
            self._call(
                "POST",
                f"/tickets/{ticket_id}/close",
                token=token,
                payload={"state": state, "resolution": resolution},
            )
        )

    def work(self, token: str | None, ticket_id: int) -> AgentSession:
        body = self._call("POST", f"/tickets/{ticket_id}/session", token=token, payload={})
        return AgentSession(
            session_id=str(body.get("session_id", "")),
            ticket_id=int(body.get("ticket_id", 0)),
            user_id=str(body.get("user_id", "")),
        )

    def approvals(self, token: str | None) -> tuple[Approval, ...]:
        body = self._call_list("GET", "/approvals", token=token)
        return tuple(_approval(row) for row in body)

    def audit(self, token: str | None, session_id: str) -> tuple[AuditRecord, ...]:
        """The audit trail for one session. Requires `audit:read`, which the API
        enforces; a caller without it gets a 403 and the page says so."""
        body = self._call_list("GET", f"/session/{session_id}/audit", token=token)
        return tuple(_audit_record(row) for row in body)

    def decide(self, token: str | None, approval_id: int, approve: bool) -> str:
        body = self._call(
            "POST",
            f"/approvals/{approval_id}/decision",
            token=token,
            payload={"approve": approve},
        )
        return str(body.get("state", ""))

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        payload: Mapping[str, Any] | None = None,
    ) -> urllib.request.Request:
        """One prepared request. Shared so that the streaming route cannot end up
        authenticating differently from the rest."""
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(dict(payload)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}", data=data, method=method, headers=headers
        )

    def _call_list(self, method: str, path: str, *, token: str | None) -> list[Mapping[str, Any]]:
        """A route that answers with a list. Separate from `_call` so neither has to
        guess which shape it is holding."""
        body = self._raw(self._request(method, path, token=token, payload=None))
        return [row for row in body if isinstance(row, dict)] if isinstance(body, list) else []

    def _call(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        body = self._raw(self._request(method, path, token=token, payload=payload))
        return body if isinstance(body, dict) else {}

    def _raw(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # `from None`: the chained exception holds `request`, and `request.headers`
            # holds the bearer token. A traceback that reached a log would carry it.
            raise _refusal(exc) from None
        except ApiError:
            raise
        except Exception:
            raise UnavailableError("The service is not answering.") from None


def _account(body: Mapping[str, Any]) -> Account:
    return Account(
        user_id=str(body.get("user_id", "")),
        roles=tuple(body.get("roles") or ()),
        permissions=tuple(body.get("permissions") or ()),
        auth=str(body.get("auth", "disabled")),
        subject=body.get("subject"),
    )


def _ticket(body: Mapping[str, Any]) -> Ticket:
    return Ticket(
        id=int(body.get("id", 0)),
        session_id=str(body.get("session_id", "")),
        user_id=str(body.get("user_id", "")),
        state=str(body.get("state", "")),
        summary=str(body.get("summary", "")),
        requested_by=str(body.get("requested_by", "")),
        assignee=body.get("assignee"),
        assignee_user_id=body.get("assignee_user_id"),
        resolution=body.get("resolution"),
        created_at=str(body.get("created_at", "")),
        closed_at=body.get("closed_at"),
    )


def _audit_record(body: Mapping[str, Any]) -> AuditRecord:
    return AuditRecord(
        kind=str(body.get("kind", "")),
        detail=dict(body.get("detail") or {}),
        operation_id=body.get("operation_id"),
        created_at=str(body.get("created_at", "")),
    )


def _approval(body: Mapping[str, Any]) -> Approval:
    return Approval(
        id=int(body.get("id", 0)),
        session_id=str(body.get("session_id", "")),
        tool_name=str(body.get("tool_name", "")),
        arguments=dict(body.get("arguments") or {}),
        requested_by=str(body.get("requested_by", "")),
        ticket_id=body.get("ticket_id"),
        expires_at=str(body.get("expires_at", "")),
    )


def _events(response: Any) -> Iterator[Event]:
    """The event stream of an open response.

    Separate from the method that opens it, and that is the point: opening happens when
    `chat_stream` is called, so a refusal is an exception the caller can retry after a
    token refresh, rather than something that surfaces on the first `next` when the
    caller has already committed to rendering a turn.
    """
    with response:
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                # Blank lines separate events; anything else is a field this client does
                # not use. Neither is an error.
                continue
            yield _event(text[len("data:") :].strip())


def _event(raw: str) -> Event:
    """One event's data field, parsed.

    A malformed event is reported as a failure rather than skipped: the stream is the
    only account of what happened, and silently dropping part of it would show a turn
    that is missing a step nobody knows about.
    """
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise UnavailableError("The service sent an event that could not be read.") from None
    if not isinstance(body, dict):
        raise UnavailableError("The service sent an event that could not be read.")

    kind = str(body.get("kind", ""))
    if kind == "error":
        raise UnavailableError(str(body.get("detail", "The turn failed."))[:MAX_DETAIL])
    if kind != "reply":
        return Event(kind=kind, tool=body.get("tool"), detail=str(body.get("detail", "")))
    return Event(
        kind="reply",
        reply=Reply(
            reply=str(body.get("reply", "")),
            status=str(body.get("status", "")),
            tool_calls=tuple(body.get("tool_calls") or ()),
            pending_confirmation=body.get("pending_confirmation"),
            stop_reason=body.get("stop_reason"),
            ticket_id=body.get("ticket_id"),
        ),
    )


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
        return ConflictError(detail or "That session is being used by another process.")
    if exc.code == 422:
        return ApiError(detail or "The service rejected that request.")
    return UnavailableError(detail or f"The service failed ({exc.code}).")
