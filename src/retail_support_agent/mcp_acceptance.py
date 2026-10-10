"""Acceptance run for the MCP server: the table in docs/M9_MCP_SERVER.md, measured.

Each scenario drives the real server over real Streamable HTTP with the SDK's own client,
once per protocol, and returns the numbers the table's "data produced" column names.
`tests/test_mcp_acceptance.py` asserts them; `scripts/run_mcp_acceptance.py` writes them
to `reports/mcp_acceptance/<date>.json`. One implementation, so the report cannot drift
from what the tests check.

No model is called and nothing is billed. The client's model is replaced by scripted
tool calls, and the user by an elicitation handler whose answer each scenario fixes.
Where a scenario is an attack, the handler accepts every form it is shown: a control
that only holds when the user happens to say no is not a control.

Needs a PostgreSQL that may be written to (`RSA_TEST_DATABASE_URL`) and the verified
retail data. Every scenario provisions fresh subjects, so runs never share a session.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, ElicitRequestParams, ElicitResult

from retail_support_agent.access_eval import EvalDomain, load_cases, run_case
from retail_support_agent.auth import AuthConfig, StaticKeys
from retail_support_agent.domain import RetailDomain, ToolResult
from retail_support_agent.mcp_server import (
    EXPOSED_TOOLS,
    CognitoTokenVerifier,
    Domain,
    RetailMcp,
    build_server,
    cancel_call,
    session_id_for,
)
from retail_support_agent.store import Store

# Both transports the SDK speaks: 2026-07-28 carries elicitation inside
# `InputRequiredResult` round trips; "legacy" negotiates 2025-11-25 by handshake and
# sends it mid-call.
MODES: tuple[str, ...] = ("2026-07-28", "legacy")

ISSUER = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_MCPACCEPT"
AUDIENCE = "mcp-acceptance-client"

# Real τ²-bench Retail records. Ava owns a pending and a delivered order and pays by gift
# card; Ivan's pending order is somebody else's data as a real row.
AVA = "ava_moore_2033"
AVA_PENDING = "#W4135875"
AVA_DELIVERED = "#W4817420"
AVA_ITEMS = ("6777246137", "4900661478")
AVA_GIFT_CARD = "gift_card_8168843"
IVAN_PENDING = "#W8770097"


# ---- transport --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serving(app: Any) -> Iterator[str]:
    """Run an ASGI app on a loopback port in a thread and yield its /mcp URL."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the MCP server under test did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def connect(url: str, mode: str, headers: dict[str, str] | None = None, **options: Any) -> Client:
    http = httpx2.AsyncClient(headers=headers or {})
    return Client(streamable_http_client(url, http_client=http), mode=mode, **options)


class Answers:
    """The user, as far as a form is concerned: one fixed answer, and a count of forms.

    The count is client-side on purpose - "elicitation sent" means a form reached the
    client, which only the client can attest.
    """

    def __init__(self, action: str = "accept", ok: bool = True) -> None:
        self.action = action
        self.ok = ok
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
        self.asked.append(params.message)
        if self.action == "accept":
            return ElicitResult(action="accept", content={"ok": self.ok})
        return ElicitResult(action=self.action)  # type: ignore[arg-type]


# ---- identity ---------------------------------------------------------------------------


class TokenMint:
    """A throwaway RS256 key pair standing in for the user pool. No network, no AWS."""

    def __init__(self, kid: str = "mcp-acceptance") -> None:
        self.kid = kid
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk]}

    def verifier(self) -> CognitoTokenVerifier:
        return CognitoTokenVerifier(
            AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(self.jwks))
        )

    def token(self, subject: str, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": subject,
            "client_id": AUDIENCE,
            "token_use": "access",
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, self._private, algorithm="RS256", headers={"kid": self.kid})

    def bearer(self, subject: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(subject)}"}


def provision(store: Store, user_id: str, *roles: str) -> str:
    subject = f"mcp-acc-{uuid.uuid4().hex[:12]}"
    store.provision_identity(subject, user_id, roles)
    return subject


# ---- the server under test --------------------------------------------------------------


class CountingDomain:
    """The real retail domain, counting the tool calls that reached it."""

    def __init__(self, inner: Domain | None = None) -> None:
        self.inner: Domain = inner if inner is not None else RetailDomain()
        self.executed: list[str] = []

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.executed.append(name)
        return self.inner.execute(name, arguments)

    def order_owner(self, order_id: str) -> str | None:
        return self.inner.order_owner(order_id)

    def order_status(self, order_id: str) -> str | None:
        return self.inner.order_status(order_id)

    def tool_names(self) -> set[str]:
        return self.inner.tool_names()

    def db_hash(self) -> str:
        return self.inner.db_hash()


@dataclass
class Rig:
    """A served MCP server, the keys its tokens are checked against, and every domain it
    has built - so a scenario can count what actually ran."""

    store: Store
    mint: TokenMint
    core: RetailMcp
    url: str
    domains: list[CountingDomain] = field(default_factory=list)

    def executions(self, since: int = 0, tool: str | None = None) -> int:
        return sum(
            1
            for domain in self.domains[since:]
            for name in domain.executed
            if tool is None or name == tool
        )


@contextmanager
def rig(
    store: Store, factory: Callable[[], Domain] | None = None, **core_options: Any
) -> Iterator[Rig]:
    mint = TokenMint()
    domains: list[CountingDomain] = []

    def build() -> CountingDomain:
        domains.append(CountingDomain(factory() if factory is not None else None))
        return domains[-1]

    core = RetailMcp(store, build, **core_options)
    server = build_server(
        core, verifier=mint.verifier(), issuer_url=ISSUER, resource_url="http://127.0.0.1/mcp"
    )
    with serving(server.streamable_http_app(host="127.0.0.1")) as url:
        yield Rig(store=store, mint=mint, core=core, url=url, domains=domains)


@asynccontextmanager
async def session(
    r: Rig, mode: str, subject: str, answers: Answers | None = None
) -> AsyncIterator[Client]:
    options: dict[str, Any] = {}
    if answers is not None:
        options["elicitation_callback"] = answers
    async with connect(r.url, mode, r.mint.bearer(subject), **options) as client:
        yield client


def outcome(result: CallToolResult) -> dict[str, Any]:
    if result.is_error or result.structured_content is None:
        return {"status": "tool_error", "content": [str(block) for block in result.content]}
    return dict(result.structured_content)


CANCEL_OWN = {"order_id": AVA_PENDING, "reason": "no longer needed"}


# ---- the ten scenarios -------------------------------------------------------------------


def _opening_request(mode: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Each protocol's first request, so a 401 is shown to land before either begins."""
    headers = {"Accept": "application/json, text/event-stream"}
    if mode == "legacy":
        return headers, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"elicitation": {}},
                "clientInfo": {"name": "acceptance", "version": "0"},
            },
        }
    headers["MCP-Protocol-Version"] = mode
    return headers, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "cancel_pending_order", "arguments": CANCEL_OWN},
    }


async def unauthenticated(r: Rig, mode: str) -> dict[str, Any]:
    """No token, an expired token, a wrong signature: 401, and no tool runs."""
    subject = provision(r.store, AVA, "customer")
    other_key = TokenMint(kid=r.mint.kid)  # same key id, different private key
    now = int(time.time())
    tokens: dict[str, str | None] = {
        "no_token": None,
        "expired_token": r.mint.token(subject, iat=now - 3600, exp=now - 600),
        "bad_signature": other_key.token(subject),
    }
    before = len(r.domains)
    codes: dict[str, int] = {}
    async with httpx2.AsyncClient() as http:
        for name, token in tokens.items():
            headers, body = _opening_request(mode)
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            codes[name] = (await http.post(r.url, headers=headers, json=body)).status_code
    numbers: dict[str, Any] = {
        "status_codes": codes,
        "tool_executions": r.executions(before),
        "sessions_opened": len(r.domains) - before,
    }
    numbers["passed"] = (
        set(codes.values()) == {401}
        and numbers["tool_executions"] == 0
        and numbers["sessions_opened"] == 0
    )
    return numbers


async def read_another_customers_order(r: Rig, mode: str) -> dict[str, Any]:
    subject = provision(r.store, AVA, "customer")
    session_id = session_id_for(subject)
    audit_before = len(r.store.audit_events(session_id))
    before = len(r.domains)
    async with session(r, mode, subject) as client:
        answer = outcome(await client.call_tool("get_order_details", {"order_id": IVAN_PENDING}))
    rows = r.store.audit_events(session_id)[audit_before:]
    numbers: dict[str, Any] = {
        "status": answer["status"],
        "refusals": int(answer["status"].startswith("denied_")),
        "new_audit_rows": len(rows),
        "new_audit_kinds": [row.kind for row in rows],
        "tool_executions": r.executions(before),
    }
    numbers["passed"] = (
        numbers["status"] == "denied_slice"
        and numbers["refusals"] == 1
        and numbers["new_audit_kinds"] == ["access_denied"]
        and numbers["tool_executions"] == 0
    )
    return numbers


async def cancel_another_customers_order(r: Rig, mode: str) -> dict[str, Any]:
    subject = provision(r.store, AVA, "customer")
    answers = Answers("accept")
    before = len(r.domains)
    async with session(r, mode, subject, answers) as client:
        args = {"order_id": IVAN_PENDING, "reason": "no longer needed"}
        answer = outcome(await client.call_tool("cancel_pending_order", args))
    numbers: dict[str, Any] = {
        "status": answer["status"],
        "elicitations_sent": len(answers.asked),
        "tool_executions": r.executions(before),
    }
    numbers["passed"] = (
        numbers["status"] == "denied_ownership"
        and numbers["elicitations_sent"] == 0
        and numbers["tool_executions"] == 0
    )
    return numbers


async def cancel_without_write_permission(r: Rig, mode: str) -> dict[str, Any]:
    """`auditor` holds `audit:read` and nothing else; the order is the account's own."""
    subject = provision(r.store, AVA, "auditor")
    answers = Answers("accept")
    before = len(r.domains)
    async with session(r, mode, subject, answers) as client:
        answer = outcome(await client.call_tool("cancel_pending_order", CANCEL_OWN))
    numbers: dict[str, Any] = {
        "status": answer["status"],
        "elicitations_sent": len(answers.asked),
        "tool_executions": r.executions(before),
    }
    numbers["passed"] = (
        numbers["status"] == "denied_permission"
        and numbers["elicitations_sent"] == 0
        and numbers["tool_executions"] == 0
    )
    return numbers


async def _answered(r: Rig, mode: str, answers: Answers) -> dict[str, Any]:
    """Load the session with a read, then make one cancellation the user answers."""
    subject = provision(r.store, AVA, "customer")
    session_id = session_id_for(subject)
    async with session(r, mode, subject, answers) as client:
        await client.call_tool("get_order_details", {"order_id": AVA_PENDING})
        hash_before = r.core.session_db_hash(subject)
        audit_before = len(r.store.audit_events(session_id))
        answer = outcome(await client.call_tool("cancel_pending_order", CANCEL_OWN))
        hash_after = r.core.session_db_hash(subject)
    executed_rows = [
        row
        for row in r.store.audit_events(session_id)[audit_before:]
        if row.kind == "write_executed"
    ]
    return {
        "status": answer["status"],
        "elicitations_sent": len(answers.asked),
        "db_hash_before": hash_before,
        "db_hash_after": hash_after,
        "db_hash_changed": hash_before != hash_after,
        "write_executed_audit_rows": len(executed_rows),
        "operations": len(r.store.operations(session_id)),
        # Whether an answered form still holds the service open (M10).
        "pending_confirmations_after": int(r.store.pending_confirmation(session_id) is not None),
    }


async def user_declines_or_cancels(r: Rig, mode: str) -> dict[str, Any]:
    declined = await _answered(r, mode, Answers("decline"))
    cancelled = await _answered(r, mode, Answers("cancel"))
    numbers: dict[str, Any] = {"decline": declined, "cancel": cancelled}
    numbers["passed"] = all(
        answer["status"] == "declined"
        and answer["elicitations_sent"] == 1
        and not answer["db_hash_changed"]
        and answer["operations"] == 0
        and answer["pending_confirmations_after"] == 0
        for answer in (declined, cancelled)
    )
    return numbers


async def user_accepts(r: Rig, mode: str) -> dict[str, Any]:
    numbers = await _answered(r, mode, Answers("accept"))
    numbers["passed"] = (
        numbers["status"] == "executed"
        and numbers["elicitations_sent"] == 1
        and numbers["db_hash_changed"]
        and numbers["write_executed_audit_rows"] == 1
        and numbers["operations"] == 1
    )
    return numbers


async def changed_argument_asks_again(r: Rig, mode: str) -> dict[str, Any]:
    """Confirm and run one return, then propose it again with another item."""
    subject = provision(r.store, AVA, "customer")
    answers = Answers("accept")
    async with session(r, mode, subject, answers) as client:
        first_args = {
            "order_id": AVA_DELIVERED,
            "item_ids": [AVA_ITEMS[0]],
            "payment_method_id": AVA_GIFT_CARD,
        }
        first = outcome(await client.call_tool("return_delivered_order_items", first_args))
        answers.action = "decline"
        second_args = {**first_args, "item_ids": [AVA_ITEMS[1]]}
        second = outcome(await client.call_tool("return_delivered_order_items", second_args))
    numbers: dict[str, Any] = {
        "first_status": first["status"],
        "second_status": second["status"],
        "elicitations_sent": len(answers.asked),
        "questions_differ": len(set(answers.asked)) == len(answers.asked),
        "operations": len(r.store.operations(session_id_for(subject))),
    }
    numbers["passed"] = (
        numbers["first_status"] == "executed"
        and numbers["second_status"] == "declined"
        and numbers["elicitations_sent"] == 2
        and numbers["questions_differ"]
        and numbers["operations"] == 1
    )
    return numbers


async def concurrent_duplicates_of_a_confirmed_call(r: Rig, mode: str) -> dict[str, Any]:
    """The user has confirmed this call and it has not run yet - a client timed out and
    retries, eight times at once. The confirmation is recorded the way the server records
    one, and the eight calls race."""
    subject = provision(r.store, AVA, "customer")
    session_id = session_id_for(subject)
    r.store.ensure_session(session_id, AVA, subject)
    call = cancel_call(AVA_PENDING, "no longer needed")
    r.store.open_confirmation(session_id, call)
    r.store.confirm(session_id, call.fingerprint)

    answers = Answers("accept")
    before = len(r.domains)
    async with session(r, mode, subject, answers) as client:
        results = await asyncio.gather(
            *(client.call_tool("cancel_pending_order", CANCEL_OWN) for _ in range(8))
        )
    answers_ = [outcome(result) for result in results]
    numbers: dict[str, Any] = {
        "calls": len(answers_),
        "executed": sum(1 for a in answers_ if a["status"] == "executed" and not a["replayed"]),
        "replayed": sum(1 for a in answers_ if a["status"] == "executed" and a["replayed"]),
        "other": sorted({a["status"] for a in answers_} - {"executed"}),
        "elicitations_sent": len(answers.asked),
        "domain_cancellations": r.executions(before, "cancel_pending_order"),
        "operations": len(r.store.operations(session_id)),
    }
    numbers["passed"] = (
        numbers["executed"] == 1
        and numbers["replayed"] == 7
        and numbers["other"] == []
        and numbers["domain_cancellations"] == 1
        and numbers["operations"] == 1
    )
    return numbers


async def client_without_elicitation(r: Rig, mode: str) -> dict[str, Any]:
    subject = provision(r.store, AVA, "customer")
    before = len(r.domains)
    async with session(r, mode, subject, answers=None) as client:
        answer = outcome(await client.call_tool("cancel_pending_order", CANCEL_OWN))
    numbers: dict[str, Any] = {
        "status": answer["status"],
        "reason": answer.get("reason", ""),
        "tool_executions": r.executions(before, "cancel_pending_order"),
        "pending_confirmations_left": int(
            r.store.pending_confirmation(session_id_for(subject)) is not None
        ),
    }
    numbers["passed"] = (
        numbers["status"] == "elicitation_unsupported"
        and "elicitation" in numbers["reason"]
        and numbers["tool_executions"] == 0
        and numbers["pending_confirmations_left"] == 0
    )
    return numbers


# ---- scenario 10: the access-control set, replayed through MCP --------------------------

# Expectations that the MCP boundary cannot observe, with the reason. They are reported
# per case as not applicable rather than counted as passed: a check that cannot fail
# proves nothing, and quietly scoring it would inflate the result.
NOT_OBSERVABLE = {
    "output_withheld": "the output check runs on the agent's own answer; over MCP the "
    "answer is written by the client's model, which this server never sees",
    "reply_excludes": "the reply is the client's model's, not this server's",
}


@dataclass
class ReplayedCase:
    case_id: str
    category: str
    outcome: str  # "blocked", "failed" or "not_applicable"
    agent_path_blocked: bool
    checks: dict[str, bool] = field(default_factory=dict)
    not_applicable: dict[str, str] = field(default_factory=dict)
    statuses: list[str] = field(default_factory=list)
    elicitations_sent: int = 0

    @property
    def consistent(self) -> bool | None:
        if self.outcome == "not_applicable":
            return None
        return (self.outcome == "blocked") == self.agent_path_blocked


async def _replay_case(store: Store, case: dict[str, Any], mode: str) -> ReplayedCase:
    domains: list[EvalDomain] = []

    def factory() -> EvalDomain:
        domains.append(EvalDomain(poison=str(case.get("poison", ""))))
        return domains[-1]

    spec = case.get("scope") or {}
    statuses = spec.get("statuses")
    options = {
        "hidden_fields": frozenset(spec.get("hidden_fields") or ()),
        "write_statuses": frozenset(statuses) if statuses else None,
    }
    calls = [step for turn in case["turns"] for step in turn["script"] if "tool" in step]
    answers = Answers("accept")  # the worst case: the user accepts every form
    shown: list[str] = []
    statuses_seen: list[str] = []
    protocol_refused: list[str] = []

    with rig(store, factory, **options) as r:
        subject = provision(store, str(case["user"]), "customer")
        async with session(r, mode, subject, answers) as client:
            for step in calls:
                name = str(step["tool"])
                try:
                    result = await client.call_tool(name, step.get("args") or {})
                except MCPError as exc:
                    protocol_refused.append(name)
                    statuses_seen.append(f"protocol_error: {exc}")
                    continue
                if result.is_error:
                    protocol_refused.append(name)
                    statuses_seen.append("tool_error")
                    shown.extend(str(block) for block in result.content)
                    continue
                answer = outcome(result)
                statuses_seen.append(str(answer["status"]))
                shown.append(json.dumps(answer, ensure_ascii=False))
        audit = [row.kind for row in store.audit_events(session_id_for(subject))]
    executed = [name for domain in domains for name in domain.executed]

    expect: dict[str, Any] = case.get("expect") or {}
    checks: dict[str, bool] = {}
    not_applicable: dict[str, str] = {}
    all_exposed = all(str(step["tool"]) in EXPOSED_TOOLS for step in calls)
    text = "\n".join(shown)

    for tool in expect.get("not_executed") or []:
        checks[f"not_executed:{tool}"] = tool not in executed
    for tool in expect.get("executed") or []:
        checks[f"executed:{tool}"] = tool in executed
    for tool in sorted({str(step["tool"]) for step in calls} - set(EXPOSED_TOOLS)):
        checks[f"refused_by_protocol:{tool}"] = tool in protocol_refused
    for kind in expect.get("audit_kinds") or []:
        if kind in NOT_OBSERVABLE:
            not_applicable[f"audit_kinds:{kind}"] = NOT_OBSERVABLE[kind]
        elif not all_exposed:
            not_applicable[f"audit_kinds:{kind}"] = (
                "the tool is not registered on the MCP server, so the call is refused by "
                "the protocol before any guard sees it"
            )
        else:
            checks[f"audit_kinds:{kind}"] = kind in audit
    if expect.get("no_audit"):
        checks["no_audit"] = audit == []
    for value in expect.get("reply_includes") or []:
        if calls:
            checks[f"server_said:{value}"] = value in text
        else:
            not_applicable[f"reply_includes:{value}"] = (
                "no tool call is made, so the text could only come from the client's model"
            )
    for value in expect.get("reply_excludes") or []:
        not_applicable[f"reply_excludes:{value}"] = NOT_OBSERVABLE["reply_excludes"]
    for value in expect.get("model_saw") or []:
        checks[f"model_saw:{value}"] = value in text
    for value in expect.get("model_never_saw") or []:
        checks[f"model_never_saw:{value}"] = value not in text

    if not calls:
        # Nothing reaches the server, so every check left would hold whatever the server
        # did - `no_audit` above all. Reported, not scored.
        for name in checks:
            not_applicable[name] = "the case makes no tool call, so nothing reaches the server"
        checks = {}
    verdict = "blocked" if all(checks.values()) else "failed"
    if not checks:
        verdict = "not_applicable"
    return ReplayedCase(
        case_id=str(case["id"]),
        category=str(case["category"]),
        outcome=verdict,
        agent_path_blocked=run_case(case).blocked,
        checks=checks,
        not_applicable=not_applicable,
        statuses=statuses_seen,
        elicitations_sent=len(answers.asked),
    )


async def access_cases_replayed(store: Store, mode: str) -> dict[str, Any]:
    """The 22 frozen access cases, their tool calls sent through MCP.

    The comparison is with the same cases run through `RetailAgent`, which is what the
    FastAPI routes drive (`access_eval.run_case`).
    """
    document = load_cases()
    replayed = [await _replay_case(store, case, mode) for case in document["cases"]]
    applicable = [case for case in replayed if case.outcome != "not_applicable"]
    numbers: dict[str, Any] = {
        "case_set": document["version"],
        "total": len(replayed),
        "applicable": len(applicable),
        "blocked": sum(1 for case in applicable if case.outcome == "blocked"),
        "not_applicable": len(replayed) - len(applicable),
        "consistent_with_agent_path": sum(1 for case in applicable if case.consistent),
        "elicitations_sent": sum(case.elicitations_sent for case in replayed),
        "cases": [
            {
                "id": case.case_id,
                "category": case.category,
                "outcome": case.outcome,
                "agent_path_blocked": case.agent_path_blocked,
                "statuses": case.statuses,
                "checks": case.checks,
                "not_applicable": case.not_applicable,
            }
            for case in replayed
        ],
    }
    numbers["passed"] = (
        numbers["blocked"] == numbers["applicable"]
        and numbers["consistent_with_agent_path"] == numbers["applicable"]
        and numbers["elicitations_sent"] == 0
    )
    return numbers


# ---- running them -----------------------------------------------------------------------

Scenario = Callable[[Rig, str], Any]

# In the order of the table in docs/M9_MCP_SERVER.md.
SCENARIOS: dict[str, Scenario] = {
    "unauthenticated": unauthenticated,
    "read_another_customers_order": read_another_customers_order,
    "cancel_another_customers_order": cancel_another_customers_order,
    "cancel_without_write_permission": cancel_without_write_permission,
    "user_declines_or_cancels": user_declines_or_cancels,
    "user_accepts": user_accepts,
    "changed_argument_asks_again": changed_argument_asks_again,
    "concurrent_duplicates_of_a_confirmed_call": concurrent_duplicates_of_a_confirmed_call,
    "client_without_elicitation": client_without_elicitation,
}
REPLAY = "access_cases_replayed"


async def run_all(store: Store, modes: tuple[str, ...] = MODES) -> dict[str, Any]:
    """Every scenario in every protocol. Returns the report body."""
    results: dict[str, dict[str, Any]] = {name: {} for name in (*SCENARIOS, REPLAY)}
    with rig(store) as r:
        for mode in modes:
            for name, scenario in SCENARIOS.items():
                results[name][mode] = await scenario(r, mode)
    for mode in modes:
        results[REPLAY][mode] = await access_cases_replayed(store, mode)
    return {
        "protocols": list(modes),
        "scenarios": results,
        "passed": all(run["passed"] for by_mode in results.values() for run in by_mode.values()),
    }


__all__ = [
    "AVA",
    "MODES",
    "REPLAY",
    "SCENARIOS",
    "Answers",
    "Rig",
    "TokenMint",
    "access_cases_replayed",
    "connect",
    "provision",
    "rig",
    "run_all",
    "serving",
]
