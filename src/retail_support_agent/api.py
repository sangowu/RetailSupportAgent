"""FastAPI service: HTTP entry point for the minimal business loop.

From M3 the service authenticates its callers when an issuer and an audience are
configured: every route then requires a bearer token, the retail identity is derived
from the token's subject, and what the caller may do comes from the database. Without
that configuration it keeps the M0 behaviour - one fixed simulated identity, loopback
only, no registration - because the evaluation loop and the local demo do not go through
an identity provider. `/health` says which mode is live.

Three refusals that must not be confused with each other:

- 401: the token is missing or does not verify. Get a new token.
- 403: the token verified, but this account may not do this, or the session belongs to
  someone else. Retrying changes nothing.
- 409 (M2): another process is driving this session. The caller has to stop sending this
  session elsewhere.

M4 adds tickets: a handoff creates one, the customer's session is suspended while it is
open, a supervisor assigns it and decides the escalations raised against it, and the
assignee works it in a session of their own bound to that ticket.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterator, Mapping
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from retail_support_agent.agent import RetailAgent, TurnEvent
from retail_support_agent.auth import (
    AuthConfig,
    AuthError,
    CachedJwks,
    Identity,
    NotAuthorizedError,
    Permission,
)
from retail_support_agent.config import Settings
from retail_support_agent.db import make_engine, make_session_factory
from retail_support_agent.domain import RetailDomain
from retail_support_agent.guardrails import SessionLimits, SessionState
from retail_support_agent.llm import ModelConfig, make_chat_fn
from retail_support_agent.scope import AccessScope
from retail_support_agent.store import Store, TicketSummary

# Routes that do not count as someone using the service. The idle watcher polls these
# once a minute; if its own polling reset the activity clock the answer would always be
# "busy" and the machine would never stop.
_QUIET_ROUTES = frozenset({"/health", "/idle"})

# Fixed demo identity from the τ²-bench simulated data, not a real user. Chosen
# because it owns both a pending order (cancellable) and a delivered order
# (returnable), so all three flows can be demonstrated under one identity.
DEMO_USER_ID = "ava_moore_2033"

# The role that works a case. Named once, because `/assignable` and the page's own
# description of what it is listing must not be able to disagree about it.
WORKING_ROLE = "agent"


def _as_payload(event: TurnEvent, ticket_id: int | None = None) -> dict[str, Any]:
    """One event, in the shape a client parses (M7.1).

    The final event is the whole of what `/chat` answers, under the same field names, so
    a client can read one branch and be a client of either route.
    """
    if event.result is None:
        return {"kind": event.kind, "tool": event.tool, "detail": event.detail}
    return {
        "kind": "reply",
        "reply": event.result.reply,
        "status": event.result.status,
        "tool_calls": list(event.result.tool_calls),
        "pending_confirmation": event.result.pending_confirmation,
        "stop_reason": event.result.stop_reason,
        "ticket_id": ticket_id,
    }


def _sse(payload: Mapping[str, Any]) -> str:
    """One server-sent event. Compact separators, because a newline inside the data
    field would end the event early."""
    body = json.dumps(dict(payload), separators=(",", ":"))
    return f"data: {body}\n\n"


# How long one process holds a session before another may take it over. It has to
# outlast a normal turn (a model call plus tool calls, seconds) and stay well under the
# 180s session limit, so a crashed process does not block a session for its whole life.
CLAIM_TTL_SECONDS = 60.0


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    reply: str
    status: str
    tool_calls: list[str] = []
    pending_confirmation: str | None = None
    stop_reason: str | None = None
    # Set when the turn handed off, or when the session is behind an open case (M7.3).
    # Read from the database rather than taken from the agent, so the agent stays exactly
    # what the benchmark measures. Before this the number existed only inside the reply's
    # prose, and a page wanting to link to the case had to dig it out of a sentence.
    ticket_id: int | None = None


class AccountResponse(BaseModel):
    """What `/me` answers: the caller's own identity as the database records it.

    `subject` is answered only by `/assignable`, where naming somebody else is the whole
    point. `/me` leaves it unset: a caller learning their own subject gains nothing, and a
    page that never sees it cannot put it somewhere it does not belong.
    """

    user_id: str
    roles: list[str] = []
    permissions: list[str] = []
    auth: str
    subject: str | None = None


class AssignRequest(BaseModel):
    assignee: str = Field(min_length=1, max_length=128)


class CloseTicketRequest(BaseModel):
    state: Literal["resolved", "rejected"]
    resolution: str = Field(default="", max_length=2000)


class DecisionRequest(BaseModel):
    approve: bool


class Ticket(BaseModel):
    id: int
    session_id: str
    user_id: str
    state: str
    summary: str
    requested_by: str | None
    assignee: str | None
    resolution: str
    created_at: str
    closed_at: str | None


class PendingApproval(BaseModel):
    """A request waiting for a human. `arguments` is what would run, so the person
    deciding sees the actual action rather than its name."""

    id: int
    session_id: str
    ticket_id: int | None
    tool_name: str
    arguments: dict[str, Any]
    requested_by: str
    expires_at: str


class SessionTimingResponse(BaseModel):
    """Elapsed time split by who was holding it up. Reporting one total would make a
    session look slower the longer a person took to answer."""

    session_id: str
    total_seconds: float
    system_seconds: float
    waiting_for_human_seconds: float


class AgentSession(BaseModel):
    session_id: str
    ticket_id: int
    user_id: str


class AuditEvent(BaseModel):
    """One audit record. No token, key or claim value appears here, by construction:
    `detail` holds the tool name, the argument fingerprint and the confirmation id."""

    kind: str
    detail: dict[str, Any]
    operation_id: int | None
    created_at: str


def make_store(settings: Settings) -> Store | None:
    """Return a Store when `RSA_DATABASE_URL` is set, otherwise None.

    Without it the service still starts and falls back to M0 in-memory sessions. The
    database is a capability, not a startup precondition: the evaluation loop has no
    PostgreSQL, and a hard dependency would stop evaluation from running at all.
    """
    if settings.database_url is None:
        return None
    engine = make_engine(settings.database_url.get_secret_value())
    return Store(make_session_factory(engine))


def make_auth(settings: Settings) -> AuthConfig | None:
    """Build the token verifier when an issuer and an audience are configured."""
    if not settings.has_auth():
        return None
    jwks_url = settings.jwks_url()
    assert settings.auth_issuer and settings.auth_audience and jwks_url  # has_auth()
    return AuthConfig(
        issuer=settings.auth_issuer,
        audience=settings.auth_audience,
        keys=CachedJwks(jwks_url),
        token_use=settings.auth_token_use or None,
    )


def _csv_set(raw: str) -> frozenset[str]:
    """A comma-separated setting as a set, ignoring blanks and surrounding spaces."""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def demo_identity(user_id: str) -> Identity:
    """The identity the service uses when authentication is not configured.

    Its subject is empty, so no session row is marked as owned and the ownership check
    stays out of the way. The `demo` role is the one place a permission is granted
    without a database row, and it is reachable only in this mode.
    """
    return Identity(subject="", user_id=user_id, roles=frozenset({"demo"}))


# The dependency and its type aliases live at module level, not inside create_app.
# With postponed annotations FastAPI resolves an endpoint's annotations against module
# globals, so a closure-local alias is invisible to it and the parameter silently
# becomes a query parameter. What the request needs from the app is read from
# `app.state` instead.
_bearer = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def resolve_identity(request: Request, credentials: BearerCredentials) -> Identity:
    """Who is calling. 401 when the token does not verify, 403 when it verifies for a
    subject this system has no account for."""
    auth: AuthConfig | None = request.app.state.auth
    if auth is None:
        fallback: Identity = request.app.state.fallback_identity
        return fallback
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="This service requires a bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        token = auth.verify(credentials.credentials)
    except AuthError as exc:
        # The message names the failed check and never the token's content.
        raise HTTPException(
            status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    store: Store | None = request.app.state.store
    assert store is not None  # guaranteed at startup: auth without a store is refused
    identity = store.identity_for(token.subject)
    if identity is None:
        # Authenticated, but not provisioned. Refusing rather than creating an account
        # on first sight is the whole point: roles are granted by an administrator,
        # never chosen by the caller.
        raise HTTPException(status_code=403, detail="This subject has no account here.")
    return identity


CurrentIdentity = Annotated[Identity, Depends(resolve_identity)]


def require(identity: Identity, permission: Permission) -> None:
    """Refuse with 403 unless the account holds the permission."""
    try:
        identity.require(permission)
    except NotAuthorizedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


class SessionBusyError(RuntimeError):
    """Another process holds this session's lease."""


class TicketAccessError(RuntimeError):
    """This session works a ticket that is not assigned to the caller.

    Separate from SessionOwnershipError because it can change without anyone doing
    anything wrong: a supervisor reassigning the case revokes access, and the message has
    to say so rather than accusing the caller of reaching into someone else's session.
    """


class SessionOwnershipError(RuntimeError):
    """This session belongs to another subject.

    Separate from SessionBusyError on purpose. Busy is about which process is driving
    the session and may resolve on its own; ownership never does, and answering both
    with one status code would tell a caller to retry a request that must not succeed.
    """


class SessionRegistry:
    """Session table. One session is processed serially.

    With a database configured, sessions, messages, confirmations and write operations
    are persisted (M1), and a session missing from this process is rebuilt from the
    database rather than started over (M2). From M3 a session also records the subject
    that opened it, and only that subject may continue it.

    Agent instances still live in the process because the domain environment is in
    memory; what M2 adds is the ability to reconstruct one.
    """

    def __init__(
        self,
        settings: Settings,
        config: ModelConfig,
        store: Store | None = None,
        owner: str | None = None,
        enforce_ownership: bool = False,
    ) -> None:
        self._settings = settings
        self._config = config
        self._store = store
        # Session ownership is enforced only when authentication is on. With it off,
        # every request is the same demo identity with an empty subject, and comparing
        # that against itself would be theatre.
        self._enforce_ownership = enforce_ownership
        # Identifies this process in the claims table. Generated per app instance, so
        # two processes never share one, and a restart does not inherit the old lease -
        # it waits for it to expire, which is the correct behaviour when the previous
        # process may still be finishing a turn.
        self._owner = owner or f"p-{uuid.uuid4().hex[:12]}"
        # Parsed once. Both are comma-separated settings, and an empty entry means the
        # slice is not narrowed on that axis.
        self._hidden_fields = _csv_set(settings.scope_redact_fields)
        self._write_statuses = _csv_set(settings.scope_write_statuses) or None
        self._agents: dict[str, RetailAgent] = {}

    @property
    def owner(self) -> str:
        return self._owner

    def get(self, session_id: str, identity: Identity) -> RetailAgent:
        """Return the session's agent, rebuilding it from the database if this process
        does not have it.

        Raises SessionOwnershipError when the session belongs to another subject, and
        SessionBusyError when another process holds the lease. Both refuse rather than
        proceed: two callers advancing one session would interleave their writes, and
        one's confirmation could authorize the other's call.
        """
        if self._store is not None:
            # Create-or-read in one transaction: the owning subject is whoever opened
            # the session, and reading it separately could miss a row a concurrent
            # request has just written.
            owner_subject = self._store.ensure_session(
                session_id, identity.user_id, identity.subject or None
            )
            if self._enforce_ownership and owner_subject != identity.subject:
                raise SessionOwnershipError(session_id)
            if not self._store.claim_session(session_id, self._owner, CLAIM_TTL_SECONDS):
                raise SessionBusyError(session_id)

        # A session bound to a ticket is an agent working a case (M4). The customer it
        # acts for comes from the ticket, never from the request, and the assignment is
        # re-read every turn so that reassigning the case ends the access at once.
        ticket_id = self._store.session_ticket(session_id) if self._store is not None else None
        acting_user = identity.user_id
        if ticket_id is not None and self._store is not None:
            if self._store.assignee_of(ticket_id) != identity.subject:
                raise TicketAccessError(session_id)
            ticket = self._store.ticket(ticket_id)
            if ticket is None or not ticket.is_open:
                raise TicketAccessError(session_id)
            acting_user = ticket.user_id

        # The slice this turn may reach. Derived from the identity the token established
        # and the assignment the database holds - never from the request - and rebuilt
        # every turn for the same reason roles are: a released case must lose its access
        # at the next message, not at the next restart.
        scope = self._scope_for(acting_user, ticket_id)

        if session_id not in self._agents:
            # Each session gets its own domain environment and database copy.
            domain = RetailDomain()
            state = SessionState(
                user_id=acting_user,
                limits=SessionLimits(),
                permissions=identity.permissions,
                scope=scope,
            )
            chat_fn = make_chat_fn(self._settings, self._config)
            agent = RetailAgent(
                chat_fn,
                domain,
                state,
                store=self._store,
                session_id=session_id,
                approval_ttl_seconds=self._settings.approval_ttl_seconds,
            )
            agent.recover()
            self._agents[session_id] = agent

        agent = self._agents[session_id]
        # Refresh the context on every turn: a cached agent would otherwise keep the
        # permissions it was built with, and a role revoked, an assignment released or an
        # approval expired would keep working until the process restarted. That is the
        # reason roles are not read from the token, so it would be strange to reintroduce
        # it here.
        agent.apply_context(
            identity.permissions,
            subject=identity.subject or None,
            ticket_id=ticket_id,
            scope=scope,
        )
        return agent

    def _scope_for(self, acting_user: str, ticket_id: int | None) -> AccessScope:
        """The access scope for one turn.

        `acting_user` is already the customer when a case is being worked, so one
        principal is right in both cases; `source` is what distinguishes them in the
        audit record, and "allowed under ticket 12" is the part a reviewer needs.
        """
        return AccessScope.for_user(
            acting_user,
            statuses=self._write_statuses,
            hidden_fields=self._hidden_fields,
            source="self" if ticket_id is None else f"ticket:{ticket_id}",
        )

    def assert_owner(self, session_id: str, identity: Identity) -> None:
        """Refuse if the session belongs to another subject.

        Separate from `get` so that dropping a session does not have to build one: an
        agent construction loads the domain environment, which is 40 ms of work to
        answer a question the database can answer directly. A session that does not
        exist is not owned by anyone, so acting on it is allowed and does nothing.
        """
        if self._store is None or not self._enforce_ownership:
            return
        exists, subject = self._store.session_subject(session_id)
        if exists and subject != identity.subject:
            raise SessionOwnershipError(session_id)

    def reset(self, session_id: str) -> None:
        self._agents.pop(session_id, None)
        if self._store is not None:
            self._store.release_session(session_id, self._owner)

    def reset_all(self) -> int:
        """Drop every session and return how many were dropped.

        Each session holds its own in-memory database copy (see `get`), so dropping
        sessions restores the demo data to the τ²-bench initial state. Nothing on
        disk is touched: evaluation results, baselines and .env are unaffected.

        The count lets the caller distinguish "reset 3 sessions" from "there were
        none". Both are success, but they are different events for an operator.
        """
        count = len(self._agents)
        if self._store is not None:
            for session_id in self._agents:
                self._store.release_session(session_id, self._owner)
        self._agents.clear()
        return count

    def active_sessions(self) -> int:
        return len(self._agents)


def create_app(
    settings: Settings | None = None,
    config: ModelConfig | None = None,
    user_id: str = DEMO_USER_ID,
    store: Store | None = None,
    auth: AuthConfig | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()  # type: ignore[call-arg]
    # An explicit store wins: tests must reach their own database, not the one in .env.
    resolved_store = store if store is not None else make_store(resolved_settings)
    resolved_auth = auth if auth is not None else make_auth(resolved_settings)

    if resolved_auth is not None and resolved_store is None:
        # Fail at startup rather than per request. With authentication on and no
        # database there is nowhere to read identities and roles from, so every request
        # would be refused; a service that cannot serve anyone should say so at boot.
        raise RuntimeError(
            "Authentication is configured but RSA_DATABASE_URL is not. "
            "Identities and roles are stored in the database."
        )

    registry = SessionRegistry(
        resolved_settings,
        config or ModelConfig(),
        store=resolved_store,
        enforce_ownership=resolved_auth is not None,
    )
    app = FastAPI(
        title="RetailSupportAgent",
        version="0.0.0",
        description="Retail support agent, minimal business loop "
        "(M4, tickets, assignment and approval on top of M3 token and role checks)",
    )
    # What the shared dependency needs, kept on the app rather than in a closure.
    app.state.auth = resolved_auth
    app.state.store = resolved_store
    app.state.fallback_identity = demo_identity(user_id)

    def session_agent(session_id: str, identity: Identity) -> RetailAgent:
        try:
            return registry.get(session_id, identity)
        except TicketAccessError as exc:
            raise HTTPException(
                status_code=403,
                detail=f"Ticket session {session_id} is not assigned to this account.",
            ) from exc
        except SessionOwnershipError as exc:
            raise HTTPException(
                status_code=403, detail=f"Session {session_id} belongs to another account."
            ) from exc
        except SessionBusyError as exc:
            # 409, not 503: the request is not going to succeed on retry against a
            # different instance either, and the caller has to stop sending this
            # session elsewhere. A 5xx would invite a retry loop.
            raise HTTPException(
                status_code=409,
                detail=f"Session {session_id} is being handled by another process.",
            ) from exc

    app.state.last_activity = time.monotonic()

    @app.middleware("http")
    async def record_activity(request: Request, call_next: Any) -> Any:
        """Stamp the time of the last request that means somebody is here.

        A monotonic clock, not a wall clock: this value is only ever compared with
        itself, and a machine whose time is corrected would otherwise appear to have
        been idle for hours or for a negative number of seconds.
        """
        if request.url.path not in _QUIET_ROUTES:
            app.state.last_activity = time.monotonic()
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "ok",
            "milestone": "M4",
            "owner": registry.owner,
            # Which mode is live. An operator must never have to guess whether the
            # service in front of them is authenticating its callers.
            "auth": "required" if resolved_auth is not None else "disabled",
            # The start/stop scripts use this to tell whether stopping now would
            # interrupt a demo in progress.
            "active_sessions": registry.active_sessions(),
            # Which image is answering. A rollback is only checkable if the running
            # version can be read rather than inferred from what was last applied.
            "image": resolved_settings.image or "unknown",
        }
        if resolved_auth is None:
            body["demo_user_id"] = user_id
        return body

    @app.get("/idle")
    def idle() -> dict[str, Any]:
        """Whether this service considers itself unused, and what is holding it open.

        The machine cannot answer this. A request blocked on a model call uses almost no
        CPU, and a confirmation nobody has answered looks like an empty machine from
        outside. So the application answers it, and the watcher on the instance acts.

        Authentication is deliberately not required. The caller is a process on the same
        machine, and the answer contains counts rather than anyone's data. Requiring a
        token would mean putting one on the instance to shut the instance down.
        """
        seconds_since_request = time.monotonic() - app.state.last_activity

        confirmations = 0
        approvals = 0
        open_tickets = 0
        if resolved_store is not None:
            confirmations = resolved_store.pending_confirmation_count()
            approvals = len(resolved_store.pending_approvals())
            open_tickets = len(resolved_store.tickets(open_only=True))

        # Each holding reason is named. A watcher that can only see a boolean cannot be
        # diagnosed when it does the wrong thing, and "why is this still running" is the
        # first question anyone asks.
        holding: list[str] = []
        if confirmations:
            holding.append(f"{confirmations} write(s) waiting for confirmation")
        if approvals:
            holding.append(f"{approvals} approval(s) waiting for a human")
        if open_tickets:
            holding.append(f"{open_tickets} open ticket(s)")

        return {
            # Idle time is time since the last request, and only counts while nothing
            # else is unfinished. Anything that closes a holding reason is itself a
            # request, so the clock restarts from that request rather than from the past.
            "idle_seconds": 0.0 if holding else round(seconds_since_request, 1),
            "seconds_since_request": round(seconds_since_request, 1),
            "holding": holding,
            # Reported, not a holding reason: SessionRegistry never releases a session,
            # so this count does not fall back to zero on its own. See M6.3 section 1.1.1.
            "active_sessions": registry.active_sessions(),
            "pending_confirmations": confirmations,
            "pending_approvals": approvals,
            "open_tickets": open_tickets,
            "persistent": resolved_store is not None,
        }

    @app.get("/me", response_model=AccountResponse)
    def me(identity: CurrentIdentity) -> AccountResponse:
        """Who the caller is, after the database has decided what they may do.

        The retail identity and the roles come from `identities` and `role_memberships`,
        never from the token's claims and never from the request. Returning them makes
        that visible: the demo renders this, so a visitor can see that signing in as one
        account does not let them act as another.

        The subject is deliberately not returned. It identifies the account in the
        identity provider and nothing on this page needs it.
        """
        return AccountResponse(
            user_id=identity.user_id,
            roles=sorted(identity.roles),
            permissions=sorted(permission.value for permission in identity.permissions),
            auth="required" if resolved_auth is not None else "disabled",
        )

    def open_ticket_id(session_id: str, status: str) -> int | None:
        """The case this session is behind, for the two statuses that have one.

        Queried only for those two, because every other turn would pay for a lookup whose
        answer is None. The agent is not asked: it would have to carry the id through a
        result the benchmark also builds, and keeping it out of there is what lets M7.1's
        claim about an untouched agent stay true.
        """
        if status not in ("handed_off", "waiting_for_human") or resolved_store is None:
            return None
        ticket = resolved_store.open_ticket_for(session_id)
        return int(ticket.id) if ticket is not None else None

    @app.post("/chat", response_model=ChatResponse)
    def chat(request: ChatRequest, identity: CurrentIdentity) -> ChatResponse:
        agent = session_agent(request.session_id, identity)
        try:
            result = agent.send(request.message)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Model or tool call failed: {exc}"
            ) from exc
        return ChatResponse(
            reply=result.reply,
            status=result.status,
            tool_calls=result.tool_calls,
            pending_confirmation=result.pending_confirmation,
            stop_reason=result.stop_reason,
            ticket_id=open_ticket_id(request.session_id, result.status),
        )

    @app.post("/chat/stream")
    def chat_stream(request: ChatRequest, identity: CurrentIdentity) -> StreamingResponse:
        """The same turn as `/chat`, reported step by step as server-sent events (M7.1).

        The agent is resolved before the response begins, so a caller who may not do this
        gets a 401, a 403 or a 409 rather than a stream that opens and then says it should
        not have. A client reading only the final event sees exactly what `/chat` returns.

        A disconnected client stops the events, not the turn: it runs to completion and
        its writes are committed and journaled, because a write already executed cannot
        be un-executed by hanging up.
        """
        agent = session_agent(request.session_id, identity)

        def events() -> Iterator[str]:
            try:
                for event in agent.stream(request.message):
                    ticket = (
                        open_ticket_id(request.session_id, event.result.status)
                        if event.result is not None
                        else None
                    )
                    yield _sse(_as_payload(event, ticket))
            except Exception as exc:
                # There is no status code left to send: the response began the moment the
                # first event went out. The failure becomes an event of its own, in the
                # shape the client already parses, carrying what a 502 would have said.
                yield _sse({"kind": "error", "detail": f"Model or tool call failed: {exc}"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            # Proxies buffer by default, which would hold every event until the turn ended
            # and undo the whole point. Caddy honours both.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/session/{session_id}/audit", response_model=list[AuditEvent])
    def audit(session_id: str, identity: CurrentIdentity) -> list[AuditEvent]:
        """The write actions recorded for one session.

        Deliberately not restricted to the session's own subject: reading someone
        else's audit trail is exactly what a supervisor role is for. What restricts it
        is the permission, which a customer does not hold.
        """
        require(identity, Permission.AUDIT_READ)
        if resolved_store is None:
            raise HTTPException(
                status_code=503, detail="The audit trail needs the application database."
            )
        return [
            AuditEvent(
                kind=row.kind,
                detail=dict(row.detail),
                operation_id=row.operation_id,
                created_at=row.created_at.isoformat(),
            )
            for row in resolved_store.audit_events(session_id)
        ]

    def need_store() -> Store:
        if resolved_store is None:
            raise HTTPException(
                status_code=503, detail="Tickets and approvals need the application database."
            )
        return resolved_store

    def as_ticket(summary: TicketSummary) -> Ticket:
        return Ticket(
            id=summary.id,
            session_id=summary.session_id,
            user_id=summary.user_id,
            state=summary.state,
            summary=summary.summary,
            requested_by=summary.requested_by,
            assignee=summary.assignee,
            resolution=summary.resolution,
            created_at=summary.created_at.isoformat(),
            closed_at=summary.closed_at.isoformat() if summary.closed_at else None,
        )

    @app.get("/tickets", response_model=list[Ticket])
    def list_tickets(identity: CurrentIdentity, open_only: bool = False) -> list[Ticket]:
        """A supervisor sees every ticket; an agent sees the ones assigned to them.

        The narrowing is applied here rather than left to the client, because "an agent
        cannot widen their own data visibility" is a rule and not a default view.
        """
        require(identity, Permission.TICKET_READ)
        store = need_store()
        assignee = None if identity.may(Permission.TICKET_ASSIGN) else identity.subject
        return [as_ticket(t) for t in store.tickets(assignee=assignee, open_only=open_only)]

    @app.post("/tickets/{ticket_id}/assign", response_model=Ticket)
    def assign_ticket(ticket_id: int, request: AssignRequest, identity: CurrentIdentity) -> Ticket:
        """Give a case to an agent. A supervisor may not take it themselves: assigning
        and deciding are the powers of this role, and performing the work as well would
        leave the decision with nobody to check it."""
        require(identity, Permission.TICKET_ASSIGN)
        store = need_store()
        if request.assignee == identity.subject:
            raise HTTPException(
                status_code=403, detail="A supervisor cannot assign a ticket to themselves."
            )
        if store.identity_for(request.assignee) is None:
            raise HTTPException(status_code=404, detail="That subject has no account here.")
        if not store.assign_ticket(ticket_id, request.assignee, identity.subject):
            raise HTTPException(status_code=409, detail="That ticket is not open.")
        assigned = store.ticket(ticket_id)
        assert assigned is not None  # assign_ticket returned True
        return as_ticket(assigned)

    @app.post("/tickets/{ticket_id}/close", response_model=Ticket)
    def close_ticket(
        ticket_id: int, request: CloseTicketRequest, identity: CurrentIdentity
    ) -> Ticket:
        """Resolve or reject a case. The assignee may close their own; a supervisor may
        close any. Closing releases the assignment and lets the customer's session run
        again."""
        require(identity, Permission.TICKET_READ)
        store = need_store()
        ticket = store.ticket(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="No such ticket.")
        if not identity.may(Permission.TICKET_ASSIGN) and ticket.assignee != identity.subject:
            raise HTTPException(status_code=403, detail="That ticket is assigned to someone else.")
        if not store.close_ticket(ticket_id, request.state, request.resolution):
            raise HTTPException(status_code=409, detail="That ticket is already closed.")
        closed = store.ticket(ticket_id)
        assert closed is not None  # close_ticket returned True
        return as_ticket(closed)

    @app.post("/tickets/{ticket_id}/session", response_model=AgentSession)
    def work_ticket(ticket_id: int, identity: CurrentIdentity) -> AgentSession:
        """Open the session in which the assignee works this case.

        The agent gets their own session rather than the customer's: the customer's
        conversation belongs to the customer, and M3's ownership check says so. The
        session id is derived from the ticket and the subject, so returning to the case
        continues it instead of starting over.
        """
        require(identity, Permission.TICKET_WORK)
        store = need_store()
        ticket = store.ticket(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="No such ticket.")
        if ticket.assignee != identity.subject:
            raise HTTPException(status_code=403, detail="That ticket is assigned to someone else.")
        if not ticket.is_open:
            raise HTTPException(status_code=409, detail="That ticket is closed.")
        digest = hashlib.sha256(identity.subject.encode()).hexdigest()[:12]
        session_id = f"t{ticket_id}-{digest}"
        store.bind_session_to_ticket(session_id, ticket.user_id, identity.subject, ticket_id)
        return AgentSession(session_id=session_id, ticket_id=ticket_id, user_id=ticket.user_id)

    @app.get("/assignable", response_model=list[AccountResponse])
    def assignable(identity: CurrentIdentity) -> list[AccountResponse]:
        """Who a case can be given to (M7.3).

        Assignment takes a subject, and a subject is an opaque identifier. Without this a
        supervisor could only assign by pasting a string they have no way to obtain, so
        the control would exist and be unusable.

        Not a new rule: it reads what the database already decided, it is gated on the
        same permission as assigning, and it answers with the same shape as `/me`. The
        subject is deliberately absent from `/me` and present here, because assigning is
        the one action that needs to name somebody else.
        """
        require(identity, Permission.TICKET_ASSIGN)
        store = need_store()
        return [
            AccountResponse(
                user_id=other.user_id,
                roles=sorted(other.roles),
                permissions=sorted(str(p) for p in other.permissions),
                auth="required",
                subject=other.subject,
            )
            for other in store.identities_with_role(WORKING_ROLE)
        ]

    @app.get("/approvals", response_model=list[PendingApproval])
    def list_approvals(identity: CurrentIdentity) -> list[PendingApproval]:
        """Escalations waiting for a decision. Expired ones are not listed: deciding one
        would produce an approval that authorizes nothing."""
        require(identity, Permission.ESCALATION_DECIDE)
        store = need_store()
        return [
            PendingApproval(
                id=int(row.id),
                session_id=row.session_id,
                ticket_id=row.ticket_id,
                tool_name=row.tool_name,
                arguments=dict(row.arguments),
                requested_by=row.requested_by,
                expires_at=row.expires_at.isoformat(),
            )
            for row in store.pending_approvals()
        ]

    @app.post("/approvals/{approval_id}/decision")
    def decide_approval(
        approval_id: int, request: DecisionRequest, identity: CurrentIdentity
    ) -> dict[str, Any]:
        """Approve or reject one escalated action.

        A rejection is final for that request: the action does not run, and nothing about
        the rejected row can authorize it later. Asking again means a new request, which
        a human decides again.
        """
        require(identity, Permission.ESCALATION_DECIDE)
        store = need_store()
        outcome = store.decide_approval(approval_id, request.approve, identity.subject)
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="No such approval request.")
        if outcome == "self_decision":
            raise HTTPException(status_code=403, detail="Nobody may decide their own escalation.")
        if outcome == "expired":
            raise HTTPException(
                status_code=409, detail="That request expired and must be raised again."
            )
        if outcome == "already_decided":
            raise HTTPException(status_code=409, detail="That request was already decided.")
        return {"approval_id": approval_id, "state": outcome}

    @app.get("/session/{session_id}/timing", response_model=SessionTimingResponse)
    def session_timing(session_id: str, identity: CurrentIdentity) -> SessionTimingResponse:
        """How long the session took, split into system time and human waiting."""
        require(identity, Permission.AUDIT_READ)
        store = need_store()
        timing = store.session_timing(session_id)
        return SessionTimingResponse(
            session_id=session_id,
            total_seconds=timing.total_seconds,
            system_seconds=timing.system_seconds,
            waiting_for_human_seconds=timing.waiting_seconds,
        )

    @app.post("/session/{session_id}/reset")
    def reset(session_id: str, identity: CurrentIdentity) -> dict[str, str]:
        # Only the session's own owner may drop it. Checked directly rather than by
        # building the agent: an unknown session must stay a cheap no-op.
        try:
            registry.assert_owner(session_id, identity)
        except SessionOwnershipError as exc:
            raise HTTPException(
                status_code=403, detail=f"Session {session_id} belongs to another account."
            ) from exc
        registry.reset(session_id)
        return {"status": "reset", "session_id": session_id}

    @app.post("/sessions/reset")
    def reset_all(identity: CurrentIdentity) -> dict[str, Any]:
        """Reset every demo session. Called by `scripts/reset-demo.ps1`.

        Clears in-process sessions only; nothing on disk is touched. `cleared` is the
        number of sessions dropped, so the caller can distinguish "reset 3 sessions"
        from "there were none".

        This one route crosses session boundaries, so it needs a permission of its own
        rather than the session's ownership check.
        """
        require(identity, Permission.SESSION_RESET_ALL)
        return {"status": "reset", "scope": "all_sessions", "cleared": registry.reset_all()}

    return app
