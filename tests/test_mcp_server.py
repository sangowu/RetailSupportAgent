"""M9: the MCP server, driven over real Streamable HTTP with the SDK's own client.

Over HTTP rather than in-process on purpose: authentication is ASGI middleware, and an
in-process client never passes through it, so a test that skipped the transport would
be testing a server with no front door.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from retail_support_agent.auth import AuthConfig, StaticKeys
from retail_support_agent.mcp_server import (
    READ_TOOLS,
    CognitoTokenVerifier,
    McpSettings,
    RetailMcp,
    build_server,
    make_verifier,
)
from test_auth import AUDIENCE, ISSUER, Keys, _make_keys, mint

# Both transports the SDK speaks: the 2026-07-28 revision, which carries elicitation in
# `InputRequiredResult` round trips, and the handshake era, which sends it mid-call.
MODES = ("2026-07-28", "legacy")


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
            raise RuntimeError("the MCP test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def connect(url: str, mode: str, headers: dict[str, str] | None = None, **options: Any) -> Client:
    http = httpx2.AsyncClient(headers=headers or {})
    return Client(streamable_http_client(url, http_client=http), mode=mode, **options)


@pytest.fixture(scope="module")
def unauthenticated_url() -> Iterator[str]:
    server = build_server(RetailMcp(store=None))
    with serving(server.streamable_http_app(host="127.0.0.1")) as url:
        yield url


@pytest.mark.parametrize("mode", MODES)
class TestTheSkeletonAnswers:
    async def test_lists_the_read_tools_under_upstream_names(
        self, unauthenticated_url: str, mode: str
    ) -> None:
        async with connect(unauthenticated_url, mode) as client:
            listed = await client.list_tools()
        assert sorted(tool.name for tool in listed.tools) == sorted(READ_TOOLS)
        for tool in listed.tools:
            assert tool.annotations is not None and tool.annotations.read_only_hint

    async def test_a_call_without_an_identity_is_refused_not_run(
        self, unauthenticated_url: str, mode: str
    ) -> None:
        async with connect(unauthenticated_url, mode) as client:
            result = await client.call_tool("get_order_details", {"order_id": "#W4135875"})
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["status"] == "denied_permission"


# ---- step 2: authentication ---------------------------------------------------------


class SpyCore(RetailMcp):
    """Records which subject reached the tool layer, and answers instead of refusing."""

    def __init__(self) -> None:
        super().__init__(store=None)
        self.calls: list[tuple[str | None, str]] = []

    def read(self, subject: str | None, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((subject, name))
        return {"status": "ok", "subject": subject}


@pytest.fixture(scope="module")
def keys() -> Keys:
    return _make_keys()


@pytest.fixture(scope="module")
def spy() -> SpyCore:
    return SpyCore()


@pytest.fixture(scope="module")
def authenticated_url(keys: Keys, spy: SpyCore) -> Iterator[str]:
    verifier = CognitoTokenVerifier(
        AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))
    )
    server = build_server(
        spy, verifier=verifier, issuer_url=ISSUER, resource_url="http://127.0.0.1/mcp"
    )
    with serving(server.streamable_http_app(host="127.0.0.1")) as url:
        yield url


def opening_request(mode: str) -> tuple[dict[str, str], dict[str, Any]]:
    """The first request a client of each protocol sends, so a refusal is shown to land
    before either protocol's own handling begins."""
    headers = {"Accept": "application/json, text/event-stream"}
    if mode == "legacy":
        return headers, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    headers["MCP-Protocol-Version"] = mode
    return headers, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_order_details", "arguments": {"order_id": "#W4135875"}},
    }


async def post_with(url: str, mode: str, token: str | None) -> int:
    headers, body = opening_request(mode)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx2.AsyncClient() as http:
        response = await http.post(url, headers=headers, json=body)
    return response.status_code


@pytest.mark.parametrize("mode", MODES)
class TestAnUnverifiedCallerNeverReachesATool:
    async def test_no_token(self, authenticated_url: str, spy: SpyCore, mode: str) -> None:
        before = len(spy.calls)
        assert await post_with(authenticated_url, mode, None) == 401
        assert len(spy.calls) == before

    async def test_an_expired_token(
        self, authenticated_url: str, keys: Keys, spy: SpyCore, mode: str
    ) -> None:
        before = len(spy.calls)
        expired = mint(keys, iat=int(time.time()) - 3600, exp=int(time.time()) - 600)
        assert await post_with(authenticated_url, mode, expired) == 401
        assert len(spy.calls) == before

    async def test_a_token_signed_by_another_key(
        self, authenticated_url: str, spy: SpyCore, mode: str
    ) -> None:
        before = len(spy.calls)
        forged = mint(_make_keys())  # same kid, different private key
        assert await post_with(authenticated_url, mode, forged) == 401
        assert len(spy.calls) == before

    async def test_a_token_for_another_application(
        self, authenticated_url: str, keys: Keys, spy: SpyCore, mode: str
    ) -> None:
        before = len(spy.calls)
        assert await post_with(authenticated_url, mode, mint(keys, client_id="other-app")) == 401
        assert len(spy.calls) == before

    async def test_a_valid_token_carries_its_subject_to_the_tool(
        self, authenticated_url: str, keys: Keys, spy: SpyCore, mode: str
    ) -> None:
        token = mint(keys, subject="subject-mcp")
        async with connect(authenticated_url, mode, {"Authorization": f"Bearer {token}"}) as client:
            result = await client.call_tool("get_order_details", {"order_id": "#W4135875"})
        assert result.structured_content == {"status": "ok", "subject": "subject-mcp"}
        assert spy.calls[-1] == ("subject-mcp", "get_order_details")


class TestTheVerifier:
    async def test_maps_a_verified_token_to_the_sdk_shape(self, keys: Keys) -> None:
        verifier = CognitoTokenVerifier(
            AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))
        )
        access = await verifier.verify_token(mint(keys, subject="s-1", scope="openid profile"))
        assert access is not None
        assert access.subject == "s-1"
        assert access.client_id == AUDIENCE
        assert access.scopes == ["openid", "profile"]
        assert access.claims == {"iss": ISSUER}

    async def test_ignores_group_claims(self, keys: Keys) -> None:
        """The token says who, never what: roles come from the database."""
        verifier = CognitoTokenVerifier(
            AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))
        )
        access = await verifier.verify_token(mint(keys, extra={"cognito:groups": ["supervisor"]}))
        assert access is not None
        assert "cognito:groups" not in (access.claims or {})

    async def test_an_id_token_is_refused(self, keys: Keys) -> None:
        verifier = CognitoTokenVerifier(
            AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))
        )
        assert await verifier.verify_token(mint(keys, token_use="id")) is None


class TestConfiguration:
    def test_the_key_set_defaults_to_the_issuers_standard_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RSA_AUTH_ISSUER", ISSUER)
        monkeypatch.setenv("RSA_AUTH_AUDIENCE", AUDIENCE)
        settings = McpSettings(_env_file=None)  # type: ignore[call-arg]
        assert settings.jwks_url() == f"{ISSUER}/.well-known/jwks.json"
        assert settings.resource_url() == "http://127.0.0.1:8765/mcp"

    def test_refuses_to_run_without_a_user_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RSA_AUTH_ISSUER", raising=False)
        monkeypatch.delenv("RSA_AUTH_AUDIENCE", raising=False)
        settings = McpSettings(_env_file=None)  # type: ignore[call-arg]
        settings.database_url = SecretStr("postgresql://x")
        with pytest.raises(SystemExit, match="requires authentication"):
            make_verifier(settings)

    def test_a_verifier_without_its_urls_is_a_configuration_error(self, keys: Keys) -> None:
        verifier = CognitoTokenVerifier(
            AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))
        )
        with pytest.raises(ValueError, match="issuer URL"):
            build_server(RetailMcp(store=None), verifier=verifier)
