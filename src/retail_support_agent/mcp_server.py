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
from collections.abc import Callable
from typing import Annotated, Any

import anyio.to_thread
import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from retail_support_agent.auth import AuthConfig, AuthError, CachedJwks
from retail_support_agent.db import make_engine, make_session_factory
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


def build_server(
    core: RetailMcp,
    *,
    verifier: TokenVerifier | None = None,
    issuer_url: str | None = None,
    resource_url: str | None = None,
) -> MCPServer:
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
    server = MCPServer(
        SERVER_NAME,
        instructions=(
            "Retail order support on simulated τ²-bench data. Reads are limited to the "
            "authenticated customer's own records."
        ),
        token_verifier=verifier,
        auth=auth,
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
    core = RetailMcp(store=Store(make_session_factory(engine)))
    server = build_server(
        core,
        verifier=verifier,
        issuer_url=settings.auth_issuer,
        resource_url=settings.resource_url(),
    )
    app = server.streamable_http_app(host=settings.host)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
