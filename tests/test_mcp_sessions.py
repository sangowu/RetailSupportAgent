"""M9 step 3: an MCP call is judged against a session assembled from the database.

The subject comes from the verified token (shown over HTTP in `test_mcp_server.py`); what
that subject may do comes from `identities` and `role_memberships`, and the slice it may
reach from the same `AccessScope` the API builds. These tests need a real PostgreSQL and
the verified retail data, like the other persistence tests.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from retail_support_agent.auth import AuthConfig, StaticKeys
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.domain import RetailDomain, ToolResult
from retail_support_agent.guardrails import ProposedCall
from retail_support_agent.mcp_acceptance import MODES, connect, serving
from retail_support_agent.mcp_server import (
    CognitoTokenVerifier,
    RetailMcp,
    build_server,
    session_id_for,
)
from retail_support_agent.scope import REDACTED
from retail_support_agent.store import Store
from test_auth import AUDIENCE, ISSUER, Keys, _make_keys, mint

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)

# Real τ²-bench Retail records. Ava owns a pending order; Ivan's pending order is
# somebody else's data as a real row rather than a missing one.
AVA = "ava_moore_2033"
AVA_PENDING = "#W4135875"
IVAN = "ivan_santos_6635"
IVAN_PENDING = "#W8770097"


class CountingDomain:
    """The real retail domain, counting the tool calls that actually reached it."""

    def __init__(self) -> None:
        self.inner = RetailDomain()
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


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    assert TEST_URL
    engine = make_engine(TEST_URL)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="module")
def keys() -> Keys:
    return _make_keys()


def provision(store: Store, user_id: str, *roles: str) -> str:
    """A fresh subject per test, so no session carries over from another test."""
    subject = f"mcp-{uuid.uuid4().hex[:10]}"
    store.provision_identity(subject, user_id, roles)
    return subject


class TestTheCoreAssemblesASessionPerCall:
    """Driven without a transport, which only has to deliver the subject."""

    def test_reads_the_customers_own_order(self, store: Store) -> None:
        subject = provision(store, AVA, "customer")
        answer = RetailMcp(store).read(subject, "get_order_details", {"order_id": AVA_PENDING})
        assert answer["status"] == "ok"
        assert json.loads(answer["result"])["user_id"] == AVA

    def test_hides_the_column_slice_from_the_client(self, store: Store) -> None:
        subject = provision(store, AVA, "customer")
        answer = RetailMcp(store).read(subject, "get_user_details", {"user_id": AVA})
        assert answer["status"] == "ok"
        assert json.loads(answer["result"])["email"] == REDACTED

    def test_somebody_elses_order_is_a_slice_refusal_on_the_record(self, store: Store) -> None:
        domains: list[CountingDomain] = []

        def factory() -> CountingDomain:
            domains.append(CountingDomain())
            return domains[-1]

        subject = provision(store, AVA, "customer")
        answer = RetailMcp(store, factory).read(
            subject, "get_order_details", {"order_id": IVAN_PENDING}
        )
        assert answer["status"] == "denied_slice"
        assert domains[0].executed == []
        rows = store.audit_events(session_id_for(subject))
        assert [row.kind for row in rows] == ["access_denied"]

    def test_somebody_elses_profile_is_refused(self, store: Store) -> None:
        subject = provision(store, AVA, "customer")
        answer = RetailMcp(store).read(subject, "get_user_details", {"user_id": IVAN})
        assert answer["status"] == "denied_slice"

    def test_the_catalog_is_open_to_every_account(self, store: Store) -> None:
        subject = provision(store, AVA, "customer")
        assert RetailMcp(store).read(subject, "list_all_product_types", {})["status"] == "ok"

    def test_an_item_is_catalog_whoever_ordered_it(self, store: Store) -> None:
        """M11: an item id names a catalog variant, not somebody's order line, so it is
        read without a data slice, like the product it belongs to."""
        subject = provision(store, AVA, "customer")
        answer = RetailMcp(store).read(subject, "get_item_details", {"item_id": "7535423717"})
        assert answer["status"] == "ok"
        item = json.loads(answer["result"])
        assert item["item_id"] == "7535423717"
        assert item["price"] == 904.46

    def test_an_unprovisioned_subject_is_refused(self, store: Store) -> None:
        answer = RetailMcp(store).read("nobody-here", "list_all_product_types", {})
        assert answer == {
            "status": "denied_permission",
            "reason": "This subject has no account here.",
        }

    def test_a_revoked_role_stops_working_on_the_next_call(self, store: Store) -> None:
        core = RetailMcp(store)
        subject = provision(store, AVA, "customer")
        own = {"order_id": AVA_PENDING}
        assert core.read(subject, "get_order_details", own)["status"] == "ok"
        store.revoke_role(subject, "customer")
        assert core.read(subject, "get_order_details", own)["status"] == "denied_permission"

    def test_repeated_probing_stops_the_session(self, store: Store) -> None:
        core = RetailMcp(store)
        subject = provision(store, AVA, "customer")
        probe = {"order_id": IVAN_PENDING}
        answers = [core.read(subject, "get_order_details", probe) for _ in range(5)]
        assert [answer["status"] for answer in answers] == ["denied_slice"] * 5
        assert "refused-action limit" in answers[-1]["reason"]
        after = core.read(subject, "get_order_details", {"order_id": AVA_PENDING})
        assert after["status"] == "stopped"

    def test_a_rebuilt_session_replays_its_writes(self, store: Store) -> None:
        """The cache is only a cache: a process without it reads the same database back."""
        subject = provision(store, AVA, "customer")
        session_id = session_id_for(subject)
        store.ensure_session(session_id, AVA, subject)
        cancel = ProposedCall(
            "cancel_pending_order", {"order_id": AVA_PENDING, "reason": "no longer needed"}
        )
        store.execute_once(session_id, cancel, None, lambda: "recorded by another process")

        answer = RetailMcp(store).read(subject, "get_order_details", {"order_id": AVA_PENDING})
        assert json.loads(answer["result"])["status"] == "cancelled"

    def test_the_cache_is_bounded(self, store: Store) -> None:
        core = RetailMcp(store, cache_size=1)
        first = provision(store, AVA, "customer")
        second = provision(store, AVA, "customer")
        core.read(first, "list_all_product_types", {})
        core.read(second, "list_all_product_types", {})
        assert core.session_db_hash(first) is None
        assert core.session_db_hash(second) is not None


@pytest.fixture(scope="module")
def url(store: Store, keys: Keys) -> Iterator[str]:
    verifier = CognitoTokenVerifier(
        AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))
    )
    server = build_server(
        RetailMcp(store), verifier=verifier, issuer_url=ISSUER, resource_url="http://x/mcp"
    )
    with serving(server.streamable_http_app(host="127.0.0.1")) as served:
        yield served


@pytest.mark.parametrize("mode", MODES)
class TestScopedReadsOverHttp:
    async def test_own_order_and_somebody_elses(
        self, url: str, store: Store, keys: Keys, mode: str
    ) -> None:
        subject = provision(store, AVA, "customer")
        headers = {"Authorization": f"Bearer {mint(keys, subject=subject)}"}
        async with connect(url, mode, headers) as client:
            own = await client.call_tool("get_order_details", {"order_id": AVA_PENDING})
            other = await client.call_tool("get_order_details", {"order_id": IVAN_PENDING})
        assert own.structured_content is not None
        assert other.structured_content is not None
        assert own.structured_content["status"] == "ok"
        assert other.structured_content["status"] == "denied_slice"
        # The refusal reads the same whether the order is somebody else's or missing.
        assert IVAN not in json.dumps(other.structured_content)
