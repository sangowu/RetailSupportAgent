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
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from retail_support_agent.agent import RetailAgent
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
from retail_support_agent.store import Store

# Fixed demo identity from the τ²-bench simulated data, not a real user. Chosen
# because it owns both a pending order (cancellable) and a delivered order
# (returnable), so all three flows can be demonstrated under one identity.
DEMO_USER_ID = "ava_moore_2033"

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

        if session_id not in self._agents:
            # Each session gets its own domain environment and database copy.
            domain = RetailDomain()
            state = SessionState(
                user_id=identity.user_id,
                limits=SessionLimits(),
                permissions=identity.permissions,
            )
            chat_fn = make_chat_fn(self._settings, self._config)
            agent = RetailAgent(chat_fn, domain, state, store=self._store, session_id=session_id)
            agent.recover()
            self._agents[session_id] = agent

        agent = self._agents[session_id]
        # Refresh permissions on every turn: a cached agent would otherwise keep the
        # set it was built with, and a role revoked in the database would keep working
        # until the process restarted. That is the reason roles are not read from the
        # token, so it would be a strange thing to reintroduce here.
        agent.apply_permissions(identity.permissions)
        return agent

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
        "(M3, token and role checks when an issuer is configured)",
    )
    # What the shared dependency needs, kept on the app rather than in a closure.
    app.state.auth = resolved_auth
    app.state.store = resolved_store
    app.state.fallback_identity = demo_identity(user_id)

    def session_agent(session_id: str, identity: Identity) -> RetailAgent:
        try:
            return registry.get(session_id, identity)
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

    @app.get("/health")
    def health() -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "ok",
            "milestone": "M3",
            "owner": registry.owner,
            # Which mode is live. An operator must never have to guess whether the
            # service in front of them is authenticating its callers.
            "auth": "required" if resolved_auth is not None else "disabled",
            # The start/stop scripts use this to tell whether stopping now would
            # interrupt a demo in progress.
            "active_sessions": registry.active_sessions(),
        }
        if resolved_auth is None:
            body["demo_user_id"] = user_id
        return body

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
