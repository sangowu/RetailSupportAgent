"""MCP server: the retail tools behind the same guard, store and scope as the API (M9).

A second entry point next to the FastAPI service, not a replacement for it. The MCP
client's own model is the agent here, so nothing in this module calls a model or goes
through the LangGraph loop. What it keeps from the application is everything that decides
whether a call may run: `WriteGuard`, `Store` and `AccessScope`. None of them is
reimplemented here.

Every refusal is returned as a tool result, `{"status": <Decision value>, "reason": ...}`,
never raised: a client model that is told why a call was refused can explain it to the
user, while an exception reaches it as an opaque error.

Run it with `uv run python -m retail_support_agent.mcp_server`.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Annotated, Any

import anyio.to_thread
import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp_types import ToolAnnotations
from pydantic import Field

from retail_support_agent.guardrails import Decision
from retail_support_agent.store import Store

logger = logging.getLogger(__name__)

SERVER_NAME = "retail-support-agent"

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

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def refusal(decision: Decision, reason: str) -> dict[str, Any]:
    """The one shape every refusal takes, so a client can branch on `status` alone."""
    return {"status": decision.value, "reason": reason}


class RetailMcp:
    """What the MCP tools do, independent of the transport.

    Synchronous on purpose: the store and the domain are synchronous, and the tool
    handlers run these methods in a worker thread so that a write waiting on the
    idempotency index never blocks the event loop that has to finish the write it is
    waiting for.
    """

    def __init__(self, store: Store | None) -> None:
        self._store = store

    def read(self, subject: str | None, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one read tool for the authenticated subject."""
        if not subject:
            return refusal(
                Decision.DENIED_PERMISSION,
                f"{name} needs an authenticated caller, and this call carries no identity.",
            )
        return refusal(
            Decision.DENIED_PERMISSION,
            f"{name} cannot run yet: this server does not resolve identities.",
        )


def _subject() -> str | None:
    """The verified token's subject, or None. Identity comes from the token only, never
    from a tool argument."""
    token = get_access_token()
    return token.subject if token is not None else None


async def _in_thread(fn: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(functools.partial(fn, *args))


def build_server(core: RetailMcp) -> MCPServer:
    """Register the tools on a new server. One server per process; tests build several."""
    server = MCPServer(
        SERVER_NAME,
        instructions=(
            "Retail order support on simulated τ²-bench data. Reads are limited to the "
            "authenticated customer's own records."
        ),
        log_level="WARNING",
    )

    @server.tool(
        description="Get the details of a user, including their orders.", annotations=READ_ONLY
    )
    async def get_user_details(user_id: Annotated[str, USER_ID]) -> dict[str, Any]:
        return await _in_thread(core.read, _subject(), "get_user_details", {"user_id": user_id})

    @server.tool(description="Get the status and details of an order.", annotations=READ_ONLY)
    async def get_order_details(order_id: Annotated[str, ORDER_ID]) -> dict[str, Any]:
        return await _in_thread(core.read, _subject(), "get_order_details", {"order_id": order_id})

    @server.tool(description="Get the inventory details of a product.", annotations=READ_ONLY)
    async def get_product_details(product_id: Annotated[str, PRODUCT_ID]) -> dict[str, Any]:
        return await _in_thread(
            core.read, _subject(), "get_product_details", {"product_id": product_id}
        )

    @server.tool(
        description="List the name and product id of all product types.", annotations=READ_ONLY
    )
    async def list_all_product_types() -> dict[str, Any]:
        return await _in_thread(core.read, _subject(), "list_all_product_types", {})

    return server


def main() -> None:
    """Serve over Streamable HTTP at /mcp on the loopback address."""
    server = build_server(RetailMcp(store=None))
    uvicorn.run(server.streamable_http_app(host="127.0.0.1"), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
