"""MCP server: the retail tools behind the same guard, store and scope as the API (M9).

A second entry point next to the FastAPI service, not a replacement for it. The MCP
client's own model is the agent here, so nothing in this module calls a model or goes
through the LangGraph loop. What it keeps from the application is everything that decides
whether a call may run: `WriteGuard`, `Store` and `AccessScope`. None of them is
reimplemented here.

Every refusal is returned as a tool result, `{"status": <Decision value>, "reason": ...}`,
never raised: a client model that is told why a call was refused can explain it to the
user, while an exception reaches it as an opaque error.

Authentication is the API's: a Cognito RS256 access token, verified by `AuthConfig`
against the pool's key set, and the subject mapped to a retail identity and roles in
the database. The token says who; it never says what they may do.

Run it with `uv run python -m retail_support_agent.mcp_server`.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import anyio.to_thread
import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from retail_support_agent.auth import AuthConfig, AuthError, CachedJwks, Identity
from retail_support_agent.db import make_engine, make_session_factory
from retail_support_agent.domain import RetailDomain, ToolResult
from retail_support_agent.guardrails import (
    Decision,
    GuardVerdict,
    ProposedCall,
    SessionLimits,
    SessionState,
    WriteGuard,
)
from retail_support_agent.handoff import hand_off, handed_off_reply, waiting_reply
from retail_support_agent.scope import AccessScope, audited
from retail_support_agent.store import Store

logger = logging.getLogger(__name__)

SERVER_NAME = "retail-support-agent"
SESSION_PREFIX = "mcp:"

# The first version's read tools. Names and parameters are upstream's, so a client that
# knows the τ²-bench Retail tools needs no translation.
READ_TOOLS: tuple[str, ...] = (
    "get_user_details",
    "get_order_details",
    "get_product_details",
    "list_all_product_types",
)

ORDER_ID = Field(description="The order id, such as '#W0000000', including the leading '#'.")
USER_ID = Field(description="The user id, such as 'sara_doe_496'.")
PRODUCT_ID = Field(
    description="The product id, such as '6086499569'. A product id is not an item id."
)

REASON = Field(
    description="The reason for cancellation: either 'no longer needed' or 'ordered by mistake'."
)
ITEM_IDS = Field(
    description="The item ids to return, each such as '1008292230'. An item may repeat."
)
PAYMENT_METHOD_ID = Field(
    description="The payment method to refund to, such as 'gift_card_0000000'. It must be "
    "the original payment method or a gift card."
)
SUMMARY = Field(description="A summary of the user's issue.")

# Said in every write tool's description, so a client model knows to expect the form
# rather than asking the user in chat and treating their "yes" as the confirmation.
CONFIRMATION_NOTE = (
    "Before it runs, the user is shown a confirmation form listing every argument; the "
    "model cannot answer it. Requires a client that supports form elicitation."
)

# Second version: exchanges, address, payment method and item changes.
WRITE_TOOLS_EXPOSED: tuple[str, ...] = ("cancel_pending_order", "return_delivered_order_items")
HANDOFF_TOOL = "transfer_to_human_agents"
EXPOSED_TOOLS: tuple[str, ...] = (*READ_TOOLS, *WRITE_TOOLS_EXPOSED, HANDOFF_TOOL)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
HANDOFF = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


class Confirm(BaseModel):
    """The confirmation form. One flat boolean: form elicitation takes primitive fields
    only, and the question above it already names every argument."""

    ok: bool = Field(description="Run this action exactly as described.")


class McpSettings(BaseSettings):
    """What the MCP server needs, and nothing else.

    Separate from `Settings` because that one requires a model key, and this process
    calls no model. The variable names are the API's, so one environment configures
    both entry points against the same database and the same user pool.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: SecretStr | None = Field(default=None, validation_alias="RSA_DATABASE_URL")
    auth_issuer: str | None = Field(default=None, validation_alias="RSA_AUTH_ISSUER")
    auth_audience: str | None = Field(default=None, validation_alias="RSA_AUTH_AUDIENCE")
    auth_jwks_url: str | None = Field(default=None, validation_alias="RSA_AUTH_JWKS_URL")
    auth_token_use: str = Field(default="access", validation_alias="RSA_AUTH_TOKEN_USE")
    host: str = Field(default="127.0.0.1", validation_alias="RSA_MCP_HOST")
    port: int = Field(default=8765, validation_alias="RSA_MCP_PORT")
    # The URL clients use to reach /mcp. Published in the protected-resource metadata;
    # defaults to the bind address, which is right for the local-only first version.
    public_url: str | None = Field(default=None, validation_alias="RSA_MCP_URL")
    # The names /mcp answers to, comma-separated. The SDK checks Host only when it binds
    # a loopback address; in a container it binds 0.0.0.0 and would accept any name, so
    # a deployment names its own. Unset, the SDK's default stands.
    allowed_hosts: str = Field(default="", validation_alias="RSA_MCP_ALLOWED_HOSTS")
    # The same slices the API applies, read from the same variables, so the two entry
    # points cannot disagree about what a customer may see or change.
    scope_redact_fields: str = Field(default="email", validation_alias="RSA_SCOPE_REDACT_FIELDS")
    scope_write_statuses: str = Field(default="", validation_alias="RSA_SCOPE_WRITE_STATUSES")
    session_cache_size: int = Field(default=32, ge=1, validation_alias="RSA_SESSION_CACHE_SIZE")

    def resource_url(self) -> str:
        return self.public_url or f"http://{self.host}:{self.port}/mcp"

    def jwks_url(self) -> str | None:
        if self.auth_jwks_url:
            return self.auth_jwks_url
        if not self.auth_issuer:
            return None
        return f"{self.auth_issuer.rstrip('/')}/.well-known/jwks.json"


class CognitoTokenVerifier:
    """The SDK's `TokenVerifier`, answered by the API's `AuthConfig.verify`.

    Signature, algorithm allowlist, issuer, expiry, audience and `token_use` are all
    checked there; this only translates the outcome. A failed check returns None, which
    the SDK's bearer middleware turns into a 401 before any tool is reached.
    """

    def __init__(self, auth: AuthConfig) -> None:
        self._auth = auth

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # In a thread: an unknown key id makes `CachedJwks` fetch the key set over
            # HTTPS, which must not stall every other request on the event loop.
            verified = await anyio.to_thread.run_sync(self._auth.verify, token)
        except AuthError as exc:
            # The message names the failed check and never the token's content.
            logger.info("MCP bearer token refused: %s", exc)
            return None
        claims = verified.claims
        audience = claims.get("client_id", claims.get("aud"))
        if isinstance(audience, list):
            audience = audience[0] if audience else None
        return AccessToken(
            token=token,
            client_id=str(audience or ""),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            subject=verified.subject,
            claims={"iss": verified.issuer},
        )


def refusal(decision: Decision, reason: str) -> dict[str, Any]:
    """The one shape every refusal takes, so a client can branch on `status` alone."""
    return {"status": decision.value, "reason": reason}


class Domain(Protocol):
    """What this module needs from a retail domain. `RetailDomain` is the real one."""

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...
    def order_owner(self, order_id: str) -> str | None: ...
    def order_status(self, order_id: str) -> str | None: ...
    def tool_names(self) -> set[str]: ...
    def db_hash(self) -> str: ...


def session_id_for(subject: str) -> str:
    """One MCP session per authenticated subject, kept apart from the API's sessions."""
    return f"{SESSION_PREFIX}{subject}"


class _CachedSession:
    """One session's retail database, held in this process.

    The lock serializes every touch of the domain: concurrent tool calls for one session
    run in different worker threads, and upstream's in-memory database is not written to
    be shared. It is never held while waiting on the store, so a write blocked on the
    idempotency index cannot hold up the write it is waiting for.
    """

    def __init__(self, domain: Domain) -> None:
        self.domain = domain
        self.lock = threading.RLock()
        # Refused proposals, counted across calls the way an agent session counts them.
        # In memory, as in the API: it resets when the entry is evicted or the process
        # restarts, which is what `SessionState.denials` does there too.
        self.denials = 0
        # Calls currently using this entry. An entry in use is never evicted: a write
        # finishing on an evicted domain would be missing from its replacement.
        self.in_use = 0

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        with self.lock:
            return self.domain.execute(name, arguments)

    def order_owner(self, order_id: str) -> str | None:
        with self.lock:
            return self.domain.order_owner(order_id)

    def order_status(self, order_id: str) -> str | None:
        with self.lock:
            return self.domain.order_status(order_id)

    def db_hash(self) -> str:
        with self.lock:
            return self.domain.db_hash()


@dataclass
class _Call:
    """Everything one tool call is judged against, assembled fresh for that call.

    Built per call rather than kept per session: the roles, the scope and the confirmed
    fingerprints are read from the database every time, so a revoked role or a
    confirmation recorded by a concurrent call is seen on the next call, not at the next
    restart.
    """

    session_id: str
    identity: Identity
    state: SessionState
    guard: WriteGuard
    cached: _CachedSession


class RetailMcp:
    """What the MCP tools do, independent of the transport.

    Synchronous on purpose: the store and the domain are synchronous, and the tool
    handlers run these methods in a worker thread so that a write waiting on the
    idempotency index never blocks the event loop that has to finish the write it is
    waiting for.
    """

    def __init__(
        self,
        store: Store | None,
        domain_factory: Callable[[], Domain] = RetailDomain,
        *,
        hidden_fields: frozenset[str] = frozenset({"email"}),
        write_statuses: frozenset[str] | None = None,
        cache_size: int = 32,
        limits: SessionLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._domain_factory = domain_factory
        self._hidden_fields = hidden_fields
        self._write_statuses = write_statuses
        self._capacity = max(1, cache_size)
        self._limits = limits or SessionLimits()
        self._sessions: OrderedDict[str, _CachedSession] = OrderedDict()
        self._sessions_guard = threading.Lock()
        self._clock = clock
        self._last_activity = clock()

    @classmethod
    def from_settings(cls, store: Store, settings: McpSettings) -> RetailMcp:
        return cls(
            store,
            hidden_fields=_csv_set(settings.scope_redact_fields),
            write_statuses=_csv_set(settings.scope_write_statuses) or None,
            cache_size=settings.session_cache_size,
        )

    # ---- tools -----------------------------------------------------------------------

    def read(self, subject: str | None, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one read tool for the authenticated subject, inside its scope."""
        with self._call(subject, name) as call:
            if isinstance(call, dict):
                return call
            verdict = self._review(call, ProposedCall(name, arguments))
            if not verdict.allowed:
                return self._refused(call, verdict)
            result = call.cached.execute(name, arguments)
            if not result.ok:
                return {"status": "tool_error", "result": result.content}
            # The column slice, applied to what the client's model is about to read.
            return {"status": "ok", "result": call.state.scope.project(result.content)}

    def propose(self, subject: str | None, proposed: ProposedCall, can_elicit: bool) -> str | None:
        """The question to put to the user before a write, or None if there is none to ask.

        Run by the elicitation resolver, before the tool body, and on 2026-07-28 once per
        round. It previews the guard's verdict on this call's own fresh state and leaves
        no trace: nothing is counted and nothing is audited, because the tool body makes
        the review that counts. Otherwise one refused write would leave two audit rows,
        or three when the protocol retries.

        Returns None unless the only thing missing is the user's confirmation: a refused
        call is refused without a form, so a confirmation can never be collected for a
        write the account may not make. The pending confirmation is recorded before the
        question goes out, and reopening it is idempotent (`Store.open_confirmation`).
        """
        with self._call(subject, proposed.name) as call:
            if isinstance(call, dict):
                return None
            verdict = call.guard.review(proposed, call.state)
            if verdict.decision is not Decision.NEEDS_CONFIRMATION or not can_elicit:
                return None
            assert self._store is not None  # a _Call exists only with a store
            self._store.open_confirmation(call.session_id, proposed)
            return proposed.describe()

    def write(
        self, subject: str | None, proposed: ProposedCall, accepted: bool, can_elicit: bool
    ) -> dict[str, Any]:
        """Run one write, if the guard allows it now.

        `accepted` is True only when the user accepted the form for exactly this call.
        Only what the database accepts counts as confirmed: `Store.confirm` refuses a
        fingerprint that is no longer the pending one (another proposal replaced it
        between the question and the answer), and the guard then still holds the call.
        """
        with self._call(subject, proposed.name) as call:
            if isinstance(call, dict):
                return call
            assert self._store is not None  # a _Call exists only with a store
            if accepted:
                self._store.confirm(call.session_id, proposed.fingerprint)
                call.state.confirmed_fingerprints = self._store.confirmed_fingerprints(
                    call.session_id
                )
            verdict = self._review(call, proposed)
            if verdict.allowed:
                return self._execute(call, proposed)
            if verdict.decision is not Decision.NEEDS_CONFIRMATION:
                return self._refused(call, verdict)
            if not can_elicit:
                return {
                    "status": "elicitation_unsupported",
                    "reason": f"{proposed.name} changes an order and needs the user's "
                    "confirmation in a form, and this client does not support form "
                    "elicitation. Nothing ran. Use a client that does.",
                }
            if accepted:
                return {
                    "status": Decision.NEEDS_CONFIRMATION.value,
                    "reason": "Another proposal replaced this one before the confirmation "
                    "arrived, so it confirms nothing. Nothing ran; call again to be asked.",
                }
            return {
                "status": "declined",
                "reason": "The user did not confirm this action. Nothing ran.",
            }

    def transfer(self, subject: str | None, summary: str) -> dict[str, Any]:
        """Hand the session to a human: upstream's transfer tool, then a ticket.

        From then on `_call` refuses every tool for this session until a human closes
        the ticket, which is the agent's rule: a case that has been escalated is not
        worked on by the model in the meantime.
        """
        with self._call(subject, HANDOFF_TOOL) as call:
            if isinstance(call, dict):
                return call
            proposed = ProposedCall(HANDOFF_TOOL, {"summary": summary})
            verdict = self._review(call, proposed)
            if not verdict.allowed:
                return self._refused(call, verdict)
            call.cached.execute(proposed.name, proposed.arguments)
            ticket = hand_off(
                self._store, call.session_id, call.identity.user_id, summary, call.identity.subject
            )
            assert ticket is not None  # a _Call exists only with a store
            return {
                "status": "handed_off",
                "ticket_id": ticket.id,
                "result": handed_off_reply(ticket),
            }

    def seconds_since_activity(self) -> float:
        """Seconds since the last tool call from an authenticated caller, or since start.

        What the idle answer is made of. A request the SDK refused with 401 never reaches
        a tool, so it never counts: otherwise anyone on the internet could keep the
        instance running by sending requests without a token.
        """
        return max(0.0, self._clock() - self._last_activity)

    def session_db_hash(self, subject: str) -> str | None:
        """The fingerprint of a session's retail database, or None if it is not loaded.
        Lets a test show that a refused or declined write changed nothing."""
        cached = self._sessions.get(session_id_for(subject))
        return cached.db_hash() if cached is not None else None

    # ---- one call --------------------------------------------------------------------

    @contextmanager
    def _call(self, subject: str | None, tool: str) -> Iterator[_Call | dict[str, Any]]:
        """Assemble the call, or yield the refusal that ends it before it starts."""
        if not subject:
            yield refusal(
                Decision.DENIED_PERMISSION,
                f"{tool} needs an authenticated caller, and this call carries no identity.",
            )
            return
        # Past authentication: this is someone using the service, whatever follows.
        self._last_activity = self._clock()
        if self._store is None:
            yield refusal(
                Decision.DENIED_PERMISSION,
                f"{tool} cannot run: this server has no database to resolve identities.",
            )
            return
        identity = self._store.identity_for(subject)
        if identity is None:
            # Authenticated but not provisioned: the API answers 403 here. An account is
            # granted by an administrator, never created on first sight.
            yield refusal(Decision.DENIED_PERMISSION, "This subject has no account here.")
            return

        session_id = session_id_for(subject)
        self._store.ensure_session(session_id, identity.user_id, subject)
        ticket = self._store.open_ticket_for(session_id)
        if ticket is not None:
            yield {"status": "waiting_for_human", "reason": waiting_reply(ticket)}
            return
        cached = self._checkout(session_id)
        try:
            state = SessionState(
                user_id=identity.user_id,
                limits=self._limits,
                permissions=identity.permissions,
                scope=AccessScope.for_user(
                    identity.user_id,
                    statuses=self._write_statuses,
                    hidden_fields=self._hidden_fields,
                    source="self",
                ),
                confirmed_fingerprints=self._store.confirmed_fingerprints(session_id),
            )
            state.denials = cached.denials
            stop = state.stop_reason()
            if stop is not None:
                yield {"status": "stopped", "reason": stop}
                return
            guard = WriteGuard(cached.domain.tool_names(), cached.order_owner, cached.order_status)
            yield _Call(session_id, identity, state, guard, cached)
        finally:
            self._release(cached)

    def _review(self, call: _Call, proposed: ProposedCall) -> GuardVerdict:
        """The verdict that counts: refusals are added to the session's tally and written
        to the audit trail. Exactly one per tool call, so one refused call is one row."""
        before = call.state.denials
        verdict = call.guard.review(proposed, call.state)
        with call.cached.lock:
            call.cached.denials += call.state.denials - before
        assert self._store is not None  # a _Call exists only with a store
        self._store.record_access_events(call.session_id, audited(call.state.slice_audit.drain()))
        return verdict

    def _execute(self, call: _Call, proposed: ProposedCall) -> dict[str, Any]:
        """Run an allowed write exactly once per confirmation.

        Keyed on the confirmation of this argument set, read from the database, so every
        caller of the same confirmed call - a retry, a concurrent duplicate, a call after
        a restart - reaches the same idempotency key, and only the first runs.
        """
        assert self._store is not None  # a _Call exists only with a store
        confirmation_id = self._store.confirmation_for(call.session_id, proposed.fingerprint)
        outcome = self._store.execute_once(
            call.session_id,
            proposed,
            confirmation_id,
            lambda: call.cached.execute(proposed.name, proposed.arguments).content,
        )
        return {
            "status": "executed",
            # Projected after the store, as the agent does: the recorded result is what a
            # rebuild replays, so it must not have holes in it.
            "result": call.state.scope.project(outcome.result),
            "replayed": outcome.replayed,
            "operation_id": outcome.operation_id,
        }

    def _refused(self, call: _Call, verdict: GuardVerdict) -> dict[str, Any]:
        reason = verdict.reason
        stop = call.state.stop_reason()
        if stop is not None:
            # Said on the refusal that reached the limit, so the client learns it now
            # rather than from the next call.
            reason = f"{reason} {stop} This session will run no further tools."
        return refusal(verdict.decision, reason)

    # ---- the per-session cache -------------------------------------------------------

    def _checkout(self, session_id: str) -> _CachedSession:
        """This session's domain, rebuilt from the database if this process lacks it."""
        with self._sessions_guard:
            cached = self._sessions.get(session_id)
            if cached is None:
                cached = _CachedSession(self._rebuild(session_id))
                self._sessions[session_id] = cached
            self._sessions.move_to_end(session_id)
            cached.in_use += 1
            self._evict_past_capacity()
            return cached

    def _release(self, cached: _CachedSession) -> None:
        with self._sessions_guard:
            cached.in_use -= 1

    def _rebuild(self, session_id: str) -> Domain:
        """A fresh domain with this session's writes replayed onto it.

        The same rule as `RetailAgent.recover`: upstream's tools are deterministic, so
        the succeeded operations, in order, reproduce the database. Replay calls the
        domain directly and never `execute_once`, so rebuilding records no new work.
        """
        assert self._store is not None
        domain = self._domain_factory()
        for op in self._store.operations(session_id):
            if op.status == "succeeded":
                domain.execute(op.tool_name, dict(op.arguments))
        return domain

    def _evict_past_capacity(self) -> None:
        """Drop least-recently-used sessions that no call is using. Each holds a copy of
        the retail database, about 12 MB, and the database can rebuild any of them."""
        while len(self._sessions) > self._capacity:
            idle = next((key for key, entry in self._sessions.items() if entry.in_use == 0), None)
            if idle is None:
                return
            del self._sessions[idle]


def _csv_set(raw: str) -> frozenset[str]:
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _subject() -> str | None:
    """The verified token's subject, or None. Identity comes from the token only, never
    from a tool argument."""
    token = get_access_token()
    return token.subject if token is not None else None


async def _in_thread(fn: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(functools.partial(fn, *args))


# The tools and their resolvers are module-level functions that find the `RetailMcp`
# through the server's lifespan, not closures over it. The SDK evaluates annotations by
# name (`inspect.signature(eval_str=True)`), so `Resolve(ask_cancel)` must name something
# a module can see.
Ctx = Context[RetailMcp, Any]


def _core(ctx: Ctx) -> RetailMcp:
    return ctx.request_context.lifespan_context


async def get_user_details(user_id: Annotated[str, USER_ID], ctx: Ctx) -> dict[str, Any]:
    return await _in_thread(_core(ctx).read, _subject(), "get_user_details", {"user_id": user_id})


async def get_order_details(order_id: Annotated[str, ORDER_ID], ctx: Ctx) -> dict[str, Any]:
    return await _in_thread(
        _core(ctx).read, _subject(), "get_order_details", {"order_id": order_id}
    )


async def get_product_details(product_id: Annotated[str, PRODUCT_ID], ctx: Ctx) -> dict[str, Any]:
    return await _in_thread(
        _core(ctx).read, _subject(), "get_product_details", {"product_id": product_id}
    )


async def list_all_product_types(ctx: Ctx) -> dict[str, Any]:
    return await _in_thread(_core(ctx).read, _subject(), "list_all_product_types", {})


# ---- writes: a resolver asks, the tool body decides ------------------------------------


async def _ask(ctx: Ctx, proposed: ProposedCall) -> Confirm | Elicit[Confirm]:
    """Put the form in front of the user, or return a `Confirm` that says nothing.

    Never returns `ok=True` itself: an accepted form is the only way the tool body sees
    one, which is what makes `accepted` mean the user said yes.
    """
    question = await anyio.to_thread.run_sync(
        _core(ctx).propose, _subject(), proposed, can_elicit(ctx)
    )
    return Confirm(ok=False) if question is None else Elicit(question, Confirm)


async def ask_cancel(order_id: str, reason: str, ctx: Ctx) -> Confirm | Elicit[Confirm]:
    return await _ask(ctx, cancel_call(order_id, reason))


async def ask_return(
    order_id: str, item_ids: list[str], payment_method_id: str, ctx: Ctx
) -> Confirm | Elicit[Confirm]:
    return await _ask(ctx, return_call(order_id, item_ids, payment_method_id))


async def cancel_pending_order(
    order_id: Annotated[str, ORDER_ID],
    reason: Annotated[str, REASON],
    ctx: Ctx,
    confirm: Annotated[ElicitationResult[Confirm], Resolve(ask_cancel)],
) -> dict[str, Any]:
    return await _in_thread(
        _core(ctx).write,
        _subject(),
        cancel_call(order_id, reason),
        accepted(confirm),
        can_elicit(ctx),
    )


async def return_delivered_order_items(
    order_id: Annotated[str, ORDER_ID],
    item_ids: Annotated[list[str], ITEM_IDS],
    payment_method_id: Annotated[str, PAYMENT_METHOD_ID],
    ctx: Ctx,
    confirm: Annotated[ElicitationResult[Confirm], Resolve(ask_return)],
) -> dict[str, Any]:
    return await _in_thread(
        _core(ctx).write,
        _subject(),
        return_call(order_id, item_ids, payment_method_id),
        accepted(confirm),
        can_elicit(ctx),
    )


async def transfer_to_human_agents(summary: Annotated[str, SUMMARY], ctx: Ctx) -> dict[str, Any]:
    return await _in_thread(_core(ctx).transfer, _subject(), summary)


# (function, description, annotations), in the order a client lists them.
TOOLS: tuple[tuple[Callable[..., Any], str, ToolAnnotations], ...] = (
    (get_user_details, "Get the details of a user, including their orders.", READ_ONLY),
    (get_order_details, "Get the status and details of an order.", READ_ONLY),
    (get_product_details, "Get the inventory details of a product.", READ_ONLY),
    (list_all_product_types, "List the name and product id of all product types.", READ_ONLY),
    (
        cancel_pending_order,
        "Cancel a pending order. A processed or delivered order cannot be cancelled. "
        + CONFIRMATION_NOTE,
        WRITE,
    ),
    (
        return_delivered_order_items,
        "Return some items of a delivered order. The order status becomes "
        "'return requested'. " + CONFIRMATION_NOTE,
        WRITE,
    ),
    (
        transfer_to_human_agents,
        "Transfer the user to a human agent, with a summary of the user's issue. Only when "
        "the user asks for a human, or the issue cannot be solved with the available tools. "
        "After this the session runs no tools until a person has closed the ticket.",
        HANDOFF,
    ),
)


def build_server(
    core: RetailMcp,
    *,
    verifier: TokenVerifier | None = None,
    issuer_url: str | None = None,
    resource_url: str | None = None,
) -> MCPServer[RetailMcp]:
    """Register the tools on a new server. One server per process; tests build several.

    With a verifier every request to /mcp needs a valid bearer token. Without one the
    server still answers, and every tool refuses: there is no identity to act for.
    """
    auth: AuthSettings | None = None
    if verifier is not None:
        if not issuer_url or not resource_url:
            raise ValueError("a token verifier needs the issuer URL and this server's URL")
        auth = AuthSettings(
            issuer_url=AnyHttpUrl(issuer_url),
            resource_server_url=AnyHttpUrl(resource_url),
            # False because the verifier already checks which application the token was
            # minted for (`client_id` or `aud`); Cognito access tokens carry no RFC 8707
            # resource indicator for the SDK to compare.
            validate_token_resource=False,
        )

    @asynccontextmanager
    async def lifespan(_: MCPServer[RetailMcp]) -> AsyncIterator[RetailMcp]:
        # The per-session domain cache lives in `core`, and `core` lives here: one
        # object for the life of the process, handed to every request.
        yield core

    server: MCPServer[RetailMcp] = MCPServer(
        SERVER_NAME,
        instructions=(
            "Retail order support on simulated τ²-bench data. Reads are limited to the "
            "authenticated customer's own records; every change to an order is confirmed "
            "by the user in a form before it runs."
        ),
        token_verifier=verifier,
        auth=auth,
        lifespan=lifespan,
        log_level="WARNING",
    )
    for fn, description, hints in TOOLS:
        server.add_tool(fn, description=description, annotations=hints)

    # Two operator routes, outside authentication (the SDK's custom routes never require
    # it) and answering with counts, never anyone's data. The proxy does not route either
    # one: /health is for compose's health check, /idle for the API's idle answer, both
    # on the compose network. Registered by call rather than as decorators, because the
    # SDK's decorator is untyped and would erase these functions' types.
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def idle(_: Request) -> JSONResponse:
        return JSONResponse({"seconds_since_request": round(core.seconds_since_activity(), 1)})

    server.custom_route("/health", methods=["GET"], include_in_schema=False)(health)
    server.custom_route("/idle", methods=["GET"], include_in_schema=False)(idle)

    return server


def transport_security(allowed_hosts: str) -> TransportSecuritySettings | None:
    """The Host and Origin check for a deployment that names its hosts, or None for the
    SDK's default (on for a loopback bind, off otherwise). A request without an Origin
    passes either way (`mcp/server/transport_security.py`, `_validate_origin`), which is
    how a non-browser client such as Claude Code arrives."""
    hosts = sorted(_csv_set(allowed_hosts))
    if not hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"https://{host}" for host in hosts],
    )


def cancel_call(order_id: str, reason: str) -> ProposedCall:
    return ProposedCall("cancel_pending_order", {"order_id": order_id, "reason": reason})


def return_call(order_id: str, item_ids: list[str], payment_method_id: str) -> ProposedCall:
    return ProposedCall(
        "return_delivered_order_items",
        {"order_id": order_id, "item_ids": list(item_ids), "payment_method_id": payment_method_id},
    )


def accepted(outcome: ElicitationResult[Confirm]) -> bool:
    """Whether the user accepted the form and ticked it."""
    return (
        isinstance(outcome, AcceptedElicitation)
        and isinstance(outcome.data, Confirm)
        and outcome.data.ok
    )


def can_elicit(ctx: Context[Any, Any]) -> bool:
    """Whether the client declared form elicitation.

    The same test the SDK applies before it sends a form (`mcp/server/mcpserver/
    resolve.py`, `_require_capability`): a bare `elicitation: {}` predates modes and
    counts as form support, a URL-only declaration does not. Checked here first so a
    client that cannot show the form gets a reason instead of a protocol error, and no
    pending confirmation is left behind for a question that was never asked.
    """
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def make_verifier(settings: McpSettings) -> CognitoTokenVerifier:
    jwks_url = settings.jwks_url()
    if not (settings.auth_issuer and settings.auth_audience and jwks_url):
        raise SystemExit(
            "The MCP server requires authentication: set RSA_AUTH_ISSUER and "
            "RSA_AUTH_AUDIENCE (and RSA_AUTH_JWKS_URL if the key set is not at the "
            "issuer's standard path)."
        )
    return CognitoTokenVerifier(
        AuthConfig(
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            keys=CachedJwks(jwks_url),
            token_use=settings.auth_token_use or None,
        )
    )


def main() -> None:
    """Serve over Streamable HTTP at /mcp.

    Refuses to start without a database or a user pool. An MCP server that answered
    anonymously would be a second front door with no lock, and the API's local
    no-authentication mode is deliberately not carried over.
    """
    settings = McpSettings()
    if settings.database_url is None:
        raise SystemExit("The MCP server requires RSA_DATABASE_URL.")
    verifier = make_verifier(settings)
    engine = make_engine(settings.database_url.get_secret_value())
    core = RetailMcp.from_settings(Store(make_session_factory(engine)), settings)
    server = build_server(
        core,
        verifier=verifier,
        issuer_url=settings.auth_issuer,
        resource_url=settings.resource_url(),
    )
    app = server.streamable_http_app(
        host=settings.host, transport_security=transport_security(settings.allowed_hosts)
    )
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
