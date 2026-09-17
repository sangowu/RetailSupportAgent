"""What the service does when the permission source is unavailable.

The design already has the right shape: roles are read from the database on every
request and nothing caches them, so there is no stale permission to fall back on. That
is easy to lose by accident - one `except Exception` around an identity lookup, one
"use the last known roles" cache added to make a slow database feel faster, and the
service starts answering questions it cannot authorize.

These tests fix the behaviour as a contract: when the permission source cannot be
reached, the request is refused. Not answered from a cache, not answered as the demo
identity, and not answered with the permissions the session had a moment ago.

They need no database. A store that raises is the whole point.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from retail_support_agent.api import DEMO_USER_ID, create_app
from retail_support_agent.auth import AuthConfig, Identity, StaticKeys
from retail_support_agent.config import Settings
from test_auth import AUDIENCE, ISSUER, Keys, bearer, mint, new_subject

RETAIL_USER = "sara_doe_496"


class PermissionSourceDownError(RuntimeError):
    """What a database outage looks like from the API's point of view."""


class FailingStore:
    """A store whose every call fails, standing in for an unreachable database.

    Deliberately not a `Store` subclass with one method patched: the question is what
    the API does when *nothing* can be read, and a partial double would answer a
    narrower question than the one being asked.
    """

    def __init__(self, succeed_first: int = 0) -> None:
        # Lets a test get one good turn in before the outage starts, which is how a
        # stale in-memory permission would show itself.
        self._remaining = succeed_first
        self.calls = 0

    def identity_for(self, subject: str) -> Identity | None:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return Identity(subject=subject, user_id=RETAIL_USER, roles=frozenset({"customer"}))
        raise PermissionSourceDownError("the permission source is unreachable")

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise PermissionSourceDownError(f"the permission source is unreachable ({name})")

        return fail


@pytest.fixture(scope="module")
def keys() -> Keys:
    from test_auth import _make_keys

    return _make_keys()


@pytest.fixture(scope="module")
def auth(keys: Keys) -> AuthConfig:
    return AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))


def make_client(store: Any, auth: AuthConfig, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    app = create_app(settings=Settings(), store=store, auth=auth)  # type: ignore[call-arg]
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def down(auth: AuthConfig, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with make_client(FailingStore(), auth, monkeypatch) as client:
        yield client


class TestAnUnreachablePermissionSourceRefuses:
    def test_an_authenticated_request_is_not_answered(self, down: TestClient, keys: Keys) -> None:
        """A verified token is not an authorization. With no source of roles there is
        nothing to authorize against, and the only safe answer is no answer."""
        response = down.get("/me", headers=bearer(mint(keys, subject=new_subject())))
        assert response.status_code >= 500
        assert response.status_code != 200

    def test_it_does_not_fall_back_to_the_demo_identity(self, down: TestClient, keys: Keys) -> None:
        """The demo identity holds order:write and session:reset_all. Reaching it
        because the database is down would be the worst possible failure mode: an
        outage that grants permissions."""
        response = down.get("/me", headers=bearer(mint(keys, subject=new_subject())))
        assert DEMO_USER_ID not in response.text
        assert RETAIL_USER not in response.text

    def test_a_write_is_not_attempted(self, down: TestClient, keys: Keys) -> None:
        response = down.post(
            "/chat",
            json={"session_id": "s-fail-secure", "message": "cancel my order"},
            headers=bearer(mint(keys, subject=new_subject())),
        )
        assert response.status_code != 200

    def test_the_refusal_does_not_leak_the_reason_to_the_caller(
        self, down: TestClient, keys: Keys
    ) -> None:
        """An outage is an internal fact. The caller learns that the request failed,
        not which component did."""
        response = down.get("/me", headers=bearer(mint(keys, subject=new_subject())))
        assert "unreachable" not in response.text.lower()
        assert "permission source" not in response.text.lower()


class TestNoStalePermissionSurvivesTheOutage:
    def test_the_second_turn_is_refused_after_the_source_goes_down(
        self, auth: AuthConfig, keys: Keys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first request resolves an identity; the outage starts after it. If the
        service kept those roles anywhere, the second request would still be answered.

        This is the test that would fail the day somebody adds an identity cache to
        make a slow database feel faster.
        """
        store = FailingStore(succeed_first=1)
        with make_client(store, auth, monkeypatch) as client:
            token = bearer(mint(keys, subject=new_subject()))
            first = client.get("/me", headers=token)
            second = client.get("/me", headers=token)

        assert first.status_code != second.status_code
        assert second.status_code >= 500
        # Both requests reached the store: the roles were re-read rather than remembered.
        assert store.calls == 2


class TestThePublicRoutesStayHonest:
    def test_health_does_not_claim_the_service_can_authorize(self, down: TestClient) -> None:
        """`/health` is the machine's own liveness probe and stays public. It must not
        be read as "authorization works": it reports the mode, and this test pins that
        it says authentication is on even while the source behind it is down."""
        response = down.get("/health")
        assert response.status_code == 200
        assert response.json()["auth"] == "required"


class TestStartupRefusesAnUnauthorizableConfiguration:
    def test_authentication_without_a_database_does_not_start(
        self, auth: AuthConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule at boot: with authentication on and nowhere to read roles
        from, every request would have to be refused, so the service says so once
        instead of failing one request at a time."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        monkeypatch.delenv("RSA_DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="RSA_DATABASE_URL"):
            create_app(settings=Settings(), store=None, auth=auth)  # type: ignore[call-arg]
