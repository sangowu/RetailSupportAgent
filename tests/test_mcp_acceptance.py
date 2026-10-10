"""The acceptance table of docs/M9_MCP_SERVER.md, one test per row and protocol.

The scenarios live in `retail_support_agent.mcp_acceptance`, which the report script
runs too, so a number in `reports/mcp_acceptance/` is one these tests assert. Each test
prints its numbers, so a failure says which one moved.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from retail_support_agent.access_eval import load_cases
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.mcp_acceptance import (
    MODES,
    SCENARIOS,
    Rig,
    _replay_case,
    access_cases_replayed,
    rig,
)
from retail_support_agent.store import Store

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (locally: docker compose up -d db)",
)


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    assert TEST_URL
    engine = make_engine(TEST_URL)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="module")
def served(store: Store) -> Iterator[Rig]:
    with rig(store) as r:
        yield r


def shown(numbers: dict[str, Any]) -> str:
    return json.dumps(numbers, indent=2, default=str)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_scenario(served: Rig, name: str, mode: str) -> None:
    numbers = await SCENARIOS[name](served, mode)
    assert numbers["passed"], shown(numbers)


@pytest.mark.parametrize("mode", MODES)
async def test_the_access_cases_hold_over_mcp(store: Store, mode: str) -> None:
    numbers = await access_cases_replayed(store, mode)
    assert numbers["passed"], shown(numbers)
    assert numbers["total"] == load_cases()["count"]
    # Pinned so that a change in what MCP can observe is a visible change to this file,
    # not a silently smaller denominator.
    assert numbers["applicable"] == 17, shown(numbers)


class TestTheReplayWouldNoticeARegression:
    """A replay that cannot fail proves nothing."""

    async def test_removing_the_column_slice_fails_its_case(self, store: Store) -> None:
        case = next(
            c
            for c in load_cases()["cases"]
            if c["id"] == "read-a-hidden-field-from-your-own-profile"
        )
        assert (await _replay_case(store, case, "legacy")).outcome == "blocked"

        weakened = {**case, "scope": {**case["scope"], "hidden_fields": []}}
        assert (await _replay_case(store, weakened, "legacy")).outcome == "failed"

    async def test_a_case_expecting_a_write_to_be_refused_fails_if_it_runs(
        self, store: Store
    ) -> None:
        """Turn the attack on Ivan's order into the customer's own order: the write now
        runs (the user accepts every form), and the case must report it."""
        case = next(c for c in load_cases()["cases"] if c["id"] == "cancel-other-users-order")
        own = json.loads(json.dumps(case).replace("#W002", "#W001"))
        assert (await _replay_case(store, own, "legacy")).outcome == "failed"
