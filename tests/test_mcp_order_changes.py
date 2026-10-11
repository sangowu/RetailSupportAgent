"""M11: the four order writes M9 left for a second version, over MCP.

Each goes through the same path as M9's two: the guard previews the call, the user
answers a form, the guard reviews it again, and the store runs it once per confirmation.
These tests drive each new tool through that path in both protocols, with real
τ²-bench records, so upstream's own checks run too.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from retail_support_agent.auth import AuthConfig, StaticKeys
from retail_support_agent.guardrails import ProposedCall
from retail_support_agent.mcp_acceptance import MODES, Answers, connect, serving
from retail_support_agent.mcp_server import (
    CognitoTokenVerifier,
    RetailMcp,
    build_server,
    exchange_call,
    modify_address_call,
    modify_payment_call,
    session_id_for,
)
from retail_support_agent.store import Store
from test_auth import AUDIENCE, ISSUER, Keys, mint
from test_mcp_sessions import AVA, AVA_PENDING, IVAN_PENDING, keys, provision, store

__all__ = ["keys", "store"]  # fixtures, imported so pytest finds them here

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)

# Real records. Raj has a pending and a delivered order, both paid by a credit card;
# Sofia has a pending order and a second credit card to move it to.
RAJ = "raj_sanchez_2970"
RAJ_PENDING = "#W4566809"
RAJ_DELIVERED = "#W7736708"
RAJ_CARD = "credit_card_3362387"
SOFIA = "sofia_li_9219"
SOFIA_PENDING = "#W8855135"
SOFIA_OTHER_CARD = "credit_card_8105988"


@dataclass(frozen=True)
class Change:
    tool: str
    user: str
    arguments: dict[str, Any]
    status_after: str

    @property
    def id(self) -> str:
        return self.tool


NEW_ADDRESS = {
    "address1": "1 Test Street",
    "address2": "",
    "city": "Austin",
    "state": "TX",
    "country": "USA",
    "zip": "73301",
}

CHANGES = (
    Change(
        "exchange_delivered_order_items",
        RAJ,
        {
            "order_id": RAJ_DELIVERED,
            "item_ids": ["3104857380"],  # headphones, on-ear, wireless, red
            "new_item_ids": ["7493556126"],  # over-ear, wireless, black
            "payment_method_id": RAJ_CARD,
        },
        "exchange requested",
    ),
    Change(
        "modify_pending_order_items",
        RAJ,
        {
            "order_id": RAJ_PENDING,
            "item_ids": ["1569829406"],  # security camera, 1080p
            "new_item_ids": ["8470360507"],  # 2K
            "payment_method_id": RAJ_CARD,
        },
        "pending (item modified)",
    ),
    Change(
        "modify_pending_order_address",
        AVA,
        {"order_id": AVA_PENDING, **NEW_ADDRESS},
        "pending",
    ),
    Change(
        "modify_pending_order_payment",
        SOFIA,
        {"order_id": SOFIA_PENDING, "payment_method_id": SOFIA_OTHER_CARD},
        "pending",
    ),
)


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


def bearer(keys: Keys, subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint(keys, subject=subject)}"}


def status_of(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    content: dict[str, Any] = result.structured_content
    return content


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("change", CHANGES, ids=lambda change: change.id)
class TestEachChange:
    async def test_accepted_runs_once_and_a_repeat_replays(
        self, url: str, store: Store, keys: Keys, change: Change, mode: str
    ) -> None:
        subject = provision(store, change.user, "customer")
        answers = Answers("accept")
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=answers) as c:
            first = status_of(await c.call_tool(change.tool, change.arguments))
            again = status_of(await c.call_tool(change.tool, change.arguments))
        assert first["status"] == "executed", first
        assert first["replayed"] is False
        order = json.loads(first["result"])
        assert order["status"] == change.status_after
        assert again["status"] == "executed"
        assert again["replayed"] is True
        assert again["operation_id"] == first["operation_id"]
        assert len(answers.asked) == 1
        assert len(store.operations(session_id_for(subject))) == 1

    async def test_the_form_explains_then_lists_every_argument(
        self, url: str, store: Store, keys: Keys, change: Change, mode: str
    ) -> None:
        """The summary comes first and the argument list, unchanged, after it: the list is
        still the statement of what runs."""
        subject = provision(store, change.user, "customer")
        answers = Answers("decline")
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=answers) as c:
            await c.call_tool(change.tool, change.arguments)
        (message,) = answers.asked
        summary, listed = message.split("\n\n")
        assert change.arguments["order_id"] in summary.splitlines()[0]
        assert listed == ProposedCall(change.tool, change.arguments).describe()

    async def test_declined_runs_nothing_and_leaves_nothing_pending(
        self, url: str, store: Store, keys: Keys, change: Change, mode: str
    ) -> None:
        subject = provision(store, change.user, "customer")
        answers = Answers("decline")
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=answers) as c:
            answer = status_of(await c.call_tool(change.tool, change.arguments))
        assert answer["status"] == "declined"
        assert len(answers.asked) == 1
        assert store.operations(session_id_for(subject)) == []
        assert store.pending_confirmation(session_id_for(subject)) is None

    async def test_another_customers_order_is_refused_without_a_form(
        self, url: str, store: Store, keys: Keys, change: Change, mode: str
    ) -> None:
        """Ivan's pending order, whatever the tool: ownership is checked before anything
        is asked, so the user is never shown a form for somebody else's order."""
        subject = provision(store, AVA if change.user != AVA else RAJ, "customer")
        answers = Answers("accept")
        arguments = {**change.arguments, "order_id": IVAN_PENDING}
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=answers) as c:
            answer = status_of(await c.call_tool(change.tool, arguments))
        assert answer["status"] == "denied_ownership"
        assert answers.asked == []
        assert store.operations(session_id_for(subject)) == []

    async def test_a_client_without_forms_is_told_and_nothing_runs(
        self, url: str, store: Store, keys: Keys, change: Change, mode: str
    ) -> None:
        subject = provision(store, change.user, "customer")
        async with connect(url, mode, bearer(keys, subject)) as c:
            answer = status_of(await c.call_tool(change.tool, change.arguments))
        assert answer["status"] == "elicitation_unsupported"
        assert store.operations(session_id_for(subject)) == []
        assert store.pending_confirmation(session_id_for(subject)) is None


class TestTheGuardDecidesAsForM9Writes:
    def test_a_write_outside_the_status_slice_asks_nothing(self, store: Store) -> None:
        """With the slice narrowed to pending orders, an exchange on a delivered order is
        refused before a form, as a cancel of a delivered order would be."""
        core = RetailMcp(store, write_statuses=frozenset({"pending"}))
        subject = provision(store, RAJ, "customer")
        call = exchange_call(RAJ_DELIVERED, ["3104857380"], ["7493556126"], RAJ_CARD)
        assert core.propose(subject, call, can_elicit=True) is None
        answer = core.write(subject, call, accepted=True, can_elicit=True)
        assert answer["status"] == "denied_slice"
        assert store.operations(session_id_for(subject)) == []

    def test_a_changed_argument_is_a_new_question(self, store: Store) -> None:
        """A confirmation covers one argument set. Accepting the address change to one
        zip code confirms nothing about another."""
        core = RetailMcp(store)
        subject = provision(store, AVA, "customer")
        asked = modify_address_call(AVA_PENDING, **NEW_ADDRESS)
        other = modify_address_call(AVA_PENDING, **{**NEW_ADDRESS, "zip": "73302"})
        assert core.propose(subject, asked, can_elicit=True) is not None
        answer = core.write(subject, other, accepted=True, can_elicit=True)
        assert answer["status"] == "needs_confirmation"
        assert store.operations(session_id_for(subject)) == []

    def test_an_upstream_refusal_after_the_form_is_reported_and_runs_nothing(
        self, store: Store
    ) -> None:
        """Upstream checks run when the call runs, after the form: moving Sofia's order to
        the card it is already paid by is refused there. The client sees upstream's
        reason, and the order is unchanged."""
        core = RetailMcp(store)
        subject = provision(store, SOFIA, "customer")
        call = modify_payment_call(SOFIA_PENDING, "credit_card_3951670")
        assert core.propose(subject, call, can_elicit=True) is not None
        answer = core.write(subject, call, accepted=True, can_elicit=True)
        assert answer["status"] == "executed"
        assert "Tool call failed" in answer["result"]
        order = core.read(subject, "get_order_details", {"order_id": SOFIA_PENDING})
        history = json.loads(order["result"])["payment_history"]
        assert [entry["payment_method_id"] for entry in history] == ["credit_card_3951670"]
