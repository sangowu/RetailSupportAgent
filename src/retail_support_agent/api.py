"""FastAPI service: HTTP entry point for the minimal business loop.

M0.3 runs with a fixed simulated identity, binds to localhost only, and offers no
registration or public access. Real authentication (Cognito) is M3.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from retail_support_agent.agent import RetailAgent
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


class SessionBusyError(RuntimeError):
    """Another process holds this session's lease."""


class SessionRegistry:
    """Session table. One session is processed serially.

    With a database configured, sessions, messages, confirmations and write operations
    are persisted (M1), and a session missing from this process is rebuilt from the
    database rather than started over (M2).

    Agent instances still live in the process because the domain environment is in
    memory; what M2 adds is the ability to reconstruct one.
    """

    def __init__(
        self,
        settings: Settings,
        config: ModelConfig,
        user_id: str,
        store: Store | None = None,
        owner: str | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._user_id = user_id
        self._store = store
        # Identifies this process in the claims table. Generated per app instance, so
        # two processes never share one, and a restart does not inherit the old lease -
        # it waits for it to expire, which is the correct behaviour when the previous
        # process may still be finishing a turn.
        self._owner = owner or f"p-{uuid.uuid4().hex[:12]}"
        self._agents: dict[str, RetailAgent] = {}

    @property
    def owner(self) -> str:
        return self._owner

    def get(self, session_id: str) -> RetailAgent:
        """Return the session's agent, rebuilding it from the database if this process
        does not have it.

        Raises SessionBusyError when another process holds the lease. Refusing is the only
        safe answer: two processes advancing one session would interleave their writes,
        and one's confirmation could authorize the other's call.
        """
        if self._store is not None and not self._store.claim_session(
            session_id, self._owner, CLAIM_TTL_SECONDS
        ):
            raise SessionBusyError(session_id)

        if session_id not in self._agents:
            # Each session gets its own domain environment and database copy.
            domain = RetailDomain()
            state = SessionState(user_id=self._user_id, limits=SessionLimits())
            chat_fn = make_chat_fn(self._settings, self._config)
            agent = RetailAgent(chat_fn, domain, state, store=self._store, session_id=session_id)
            agent.recover()
            self._agents[session_id] = agent
        return self._agents[session_id]

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
) -> FastAPI:
    resolved_settings = settings or Settings()  # type: ignore[call-arg]
    # An explicit store wins: tests must reach their own database, not the one in .env.
    resolved_store = store if store is not None else make_store(resolved_settings)
    registry = SessionRegistry(
        resolved_settings, config or ModelConfig(), user_id, store=resolved_store
    )

    app = FastAPI(
        title="RetailSupportAgent",
        version="0.0.0",
        description="Retail support agent, minimal business loop "
        "(M0.3, fixed simulated identity, local only)",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "milestone": "M2",
            "owner": registry.owner,
            "demo_user_id": user_id,
            # The start/stop scripts use this to tell whether stopping now would
            # interrupt a demo in progress.
            "active_sessions": registry.active_sessions(),
        }

    @app.post("/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        try:
            agent = registry.get(request.session_id)
        except SessionBusyError as exc:
            # 409, not 503: the request is not going to succeed on retry against a
            # different instance either, and the caller has to stop sending this
            # session elsewhere. A 5xx would invite a retry loop.
            raise HTTPException(
                status_code=409,
                detail=f"Session {request.session_id} is being handled by another process.",
            ) from exc
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

    @app.post("/session/{session_id}/reset")
    def reset(session_id: str) -> dict[str, str]:
        registry.reset(session_id)
        return {"status": "reset", "session_id": session_id}

    @app.post("/sessions/reset")
    def reset_all() -> dict[str, Any]:
        """Reset every demo session. Called by `scripts/reset-demo.ps1`.

        Clears in-process sessions only; nothing on disk is touched. `cleared` is the
        number of sessions dropped, so the caller can distinguish "reset 3 sessions"
        from "there were none".
        """
        return {"status": "reset", "scope": "all_sessions", "cleared": registry.reset_all()}

    return app
