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

from retail_support_agent.mcp_server import READ_TOOLS, RetailMcp, build_server

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
