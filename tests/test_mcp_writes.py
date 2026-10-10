"""M9 step 4: writes over MCP need the user's answer in a form, and run once.

The acceptance table in docs/M9_MCP_SERVER.md is measured in `test_mcp_acceptance.py`.
These tests cover what the table does not name: that a client cannot answer for the
user by passing the answer as an argument, that a confirmation another proposal
replaced confirms nothing, that the handoff suspends the session, and that the tools
keep upstream's names and parameters.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from mcp_types import ElicitRequestParams, ElicitResult

from retail_support_agent.auth import AuthConfig, StaticKeys
from retail_support_agent.domain import RetailDomain
from retail_support_agent.mcp_server import (
    EXPOSED_TOOLS,
    CognitoTokenVerifier,
    RetailMcp,
    build_server,
    cancel_call,
    session_id_for,
)
from retail_support_agent.store import Store
from test_auth import AUDIENCE, ISSUER, Keys, mint
from test_mcp_server import MODES, connect, serving
from test_mcp_sessions import AVA, AVA_PENDING, keys, provision, store

__all__ = ["keys", "store"]  # fixtures, imported so pytest finds them here

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)


class Answers:
    """A client-side elicitation handler: answers every form the same way, and counts."""

    def __init__(self, action: str = "accept", ok: bool = True) -> None:
        self.action = action
        self.ok = ok
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
        self.asked.append(params.message)
        if self.action == "accept":
            return ElicitResult(action="accept", content={"ok": self.ok})
        return ElicitResult(action=self.action)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def core(store: Store) -> RetailMcp:
    return RetailMcp(store)


@pytest.fixture(scope="module")
def url(core: RetailMcp, keys: Keys) -> Iterator[str]:
    verifier = CognitoTokenVerifier(
        AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))
    )
    server = build_server(core, verifier=verifier, issuer_url=ISSUER, resource_url="http://x/mcp")
    with serving(server.streamable_http_app(host="127.0.0.1")) as served:
        yield served


def bearer(keys: Keys, subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint(keys, subject=subject)}"}


def status_of(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    content: dict[str, Any] = result.structured_content
    return content


CANCEL_OWN = {"order_id": AVA_PENDING, "reason": "no longer needed"}


@pytest.mark.parametrize("mode", MODES)
class TestAConfirmedWrite:
    async def test_accepting_the_form_runs_the_write(
        self, url: str, store: Store, keys: Keys, mode: str
    ) -> None:
        subject = provision(store, AVA, "customer")
        answers = Answers("accept")
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=answers) as c:
            answer = status_of(await c.call_tool("cancel_pending_order", CANCEL_OWN))
        assert answer["status"] == "executed"
        assert answer["replayed"] is False
        assert json.loads(answer["result"])["status"] == "cancelled"
        # The form names every argument, so the user confirms this call and no other.
        assert answers.asked == [cancel_call(AVA_PENDING, "no longer needed").describe()]

    async def test_an_unticked_form_is_a_refusal(
        self, url: str, store: Store, keys: Keys, mode: str
    ) -> None:
        subject = provision(store, AVA, "customer")
        answers = Answers("accept", ok=False)
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=answers) as c:
            answer = status_of(await c.call_tool("cancel_pending_order", CANCEL_OWN))
        assert answer["status"] == "declined"
        assert store.operations(session_id_for(subject)) == []

    async def test_a_client_cannot_pass_the_answer_as_an_argument(
        self, url: str, store: Store, keys: Keys, mode: str
    ) -> None:
        """The resolved parameter is not part of the input schema. A model that writes
        the answer into the arguments is still shown the form - here, declined."""
        subject = provision(store, AVA, "customer")
        answers = Answers("decline")
        forged = {**CANCEL_OWN, "confirm": {"action": "accept", "data": {"ok": True}}}
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=answers) as c:
            result = await c.call_tool("cancel_pending_order", forged)
        assert status_of(result)["status"] == "declined"
        assert len(answers.asked) == 1
        assert store.operations(session_id_for(subject)) == []

    async def test_a_refused_write_leaves_one_audit_row_whatever_the_protocol(
        self, url: str, store: Store, keys: Keys, mode: str
    ) -> None:
        """The resolver previews the verdict, on 2026-07-28 once per round; only the tool
        body's review is recorded."""
        subject = provision(store, AVA, "customer")
        other = {"order_id": "#W8770097", "reason": "no longer needed"}
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=Answers()) as c:
            answer = status_of(await c.call_tool("cancel_pending_order", other))
        assert answer["status"] == "denied_ownership"
        kinds = [row.kind for row in store.audit_events(session_id_for(subject))]
        assert kinds == ["access_denied"]

    async def test_a_handoff_suspends_the_session(
        self, url: str, store: Store, keys: Keys, mode: str
    ) -> None:
        subject = provision(store, AVA, "customer")
        async with connect(url, mode, bearer(keys, subject), elicitation_callback=Answers()) as c:
            handed = status_of(
                await c.call_tool("transfer_to_human_agents", {"summary": "wants a person"})
            )
            after_read = status_of(
                await c.call_tool("get_order_details", {"order_id": AVA_PENDING})
            )
            after_write = status_of(await c.call_tool("cancel_pending_order", CANCEL_OWN))
        assert handed["status"] == "handed_off"
        ticket = store.open_ticket_for(session_id_for(subject))
        assert ticket is not None and ticket.id == handed["ticket_id"]
        assert ticket.summary == "wants a person"
        assert after_read["status"] == "waiting_for_human"
        assert after_write["status"] == "waiting_for_human"
        assert store.operations(session_id_for(subject)) == []


class TestTheConfirmationIsTheDatabases:
    def test_a_replaced_proposal_confirms_nothing(self, core: RetailMcp, store: Store) -> None:
        """Asked about one call, and before the answer arrived another proposal took the
        pending slot. The answer to the first question must not run the first call."""
        subject = provision(store, AVA, "customer")
        first = cancel_call(AVA_PENDING, "no longer needed")
        second = cancel_call(AVA_PENDING, "ordered by mistake")
        assert core.propose(subject, first, can_elicit=True) is not None
        assert core.propose(subject, second, can_elicit=True) is not None

        answer = core.write(subject, first, accepted=True, can_elicit=True)
        assert answer["status"] == "needs_confirmation"
        assert store.operations(session_id_for(subject)) == []

    def test_asking_again_keeps_the_same_pending_confirmation(
        self, core: RetailMcp, store: Store
    ) -> None:
        subject = provision(store, AVA, "customer")
        call = cancel_call(AVA_PENDING, "no longer needed")
        core.propose(subject, call, can_elicit=True)
        first = store.pending_confirmation(session_id_for(subject))
        core.propose(subject, call, can_elicit=True)
        again = store.pending_confirmation(session_id_for(subject))
        assert first is not None and again is not None
        assert again.id == first.id

    def test_a_refused_call_opens_no_confirmation(self, core: RetailMcp, store: Store) -> None:
        subject = provision(store, AVA, "customer")
        other = cancel_call("#W8770097", "no longer needed")
        assert core.propose(subject, other, can_elicit=True) is None
        assert store.pending_confirmation(session_id_for(subject)) is None


@pytest.fixture(scope="module")
def upstream() -> dict[str, dict[str, Any]]:
    return {
        schema["function"]["name"]: schema["function"]["parameters"]
        for schema in RetailDomain().tool_schemas()
    }


class TestTheToolsAreUpstreams:
    async def test_every_exposed_tool_has_upstreams_parameters(
        self, upstream: dict[str, dict[str, Any]]
    ) -> None:
        """The confirmation and the context are filled by the server and must not appear
        as parameters a client model could set."""
        listed = {tool.name: tool for tool in await build_server(RetailMcp(None)).list_tools()}
        assert set(listed) == set(EXPOSED_TOOLS)
        for name, tool in listed.items():
            ours = tool.input_schema
            theirs = upstream[name]
            assert set(ours.get("properties", {})) == set(theirs.get("properties", {})), name
            assert set(ours.get("required", [])) == set(theirs.get("required", [])), name

    async def test_every_write_says_it_will_ask(self) -> None:
        for tool in await build_server(RetailMcp(None)).list_tools():
            if tool.annotations is not None and tool.annotations.destructive_hint:
                assert "confirmation form" in (tool.description or ""), tool.name
