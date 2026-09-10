"""M3: token verification, roles, and who may touch what.

One class per acceptance threshold in docs/M3_AUTH.md §1.2.

The token tests need no network and no AWS: a key pair is generated in the fixture, the
JWKS document is built from its public half, and tokens are minted here. That is the
whole point of the design - the verifier is exercised against real RS256 signatures, and
pointing it at a Cognito user pool is configuration.

The tests that involve identities and sessions need a real PostgreSQL, because that is
where identities, roles and session ownership live. Locally: `docker compose up -d db`,
then point `RSA_TEST_DATABASE_URL` at it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from retail_support_agent.api import DEMO_USER_ID, ChatRequest, create_app
from retail_support_agent.auth import (
    AuthConfig,
    AuthError,
    CachedJwks,
    Identity,
    NotAuthorizedError,
    Permission,
    StaticKeys,
    permissions_for,
)
from retail_support_agent.config import Settings
from retail_support_agent.db import Base, make_engine, make_session_factory
from retail_support_agent.guardrails import Decision, ProposedCall, SessionState, WriteGuard
from retail_support_agent.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# Imported after the path insert above, the way the other script tests do it.
from provision_identity import main as provision_main

TEST_URL = os.getenv("RSA_TEST_DATABASE_URL")

ISSUER = "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_TESTPOOL"
AUDIENCE = "test-app-client-id"
KID = "test-key-1"
RETAIL_USER = "ava_moore_2033"


# ---- key material and token minting -------------------------------------


@dataclass(frozen=True)
class Keys:
    """A key pair plus the JWKS document a verifier would fetch for it."""

    private_pem: str
    public_pem: str
    jwks: dict[str, Any]


def _make_keys(kid: str = KID) -> Keys:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return Keys(private_pem=private_pem, public_pem=public_pem, jwks={"keys": [jwk]})


@pytest.fixture(scope="module")
def keys() -> Keys:
    return _make_keys()


@pytest.fixture(scope="module")
def auth(keys: Keys) -> AuthConfig:
    return AuthConfig(issuer=ISSUER, audience=AUDIENCE, keys=StaticKeys(keys.jwks))


def mint(
    keys: Keys,
    subject: str = "subject-a",
    kid: str = KID,
    extra: dict[str, Any] | None = None,
    **overrides: Any,
) -> str:
    """A token shaped like a Cognito access token, with any claim overridden.

    `extra` carries claims whose names are not Python identifiers, `cognito:groups`
    above all - the claim this design deliberately ignores.
    """
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
    claims.update(extra or {})
    claims = {key: value for key, value in claims.items() if value is not None}
    return jwt.encode(claims, keys.private_pem, algorithm="RS256", headers={"kid": kid})


def _b64(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


# ---- 1. token verification ----------------------------------------------


class TestTokenVerification:
    """Threshold: every malformed, expired, misdirected or forged token is refused."""

    def test_a_valid_token_verifies(self, auth: AuthConfig, keys: Keys) -> None:
        token = auth.verify(mint(keys))
        assert token.subject == "subject-a"
        assert token.issuer == ISSUER

    def test_an_expired_token_is_refused(self, auth: AuthConfig, keys: Keys) -> None:
        now = int(time.time())
        # Well past the 30s leeway, which exists for clock skew and not as a grace period.
        with pytest.raises(AuthError):
            auth.verify(mint(keys, iat=now - 7200, exp=now - 3600))

    def test_another_issuers_token_is_refused(self, auth: AuthConfig, keys: Keys) -> None:
        with pytest.raises(AuthError):
            auth.verify(mint(keys, iss="https://accounts.example.com/other-pool"))

    def test_another_applications_token_is_refused(self, auth: AuthConfig, keys: Keys) -> None:
        """A token for a different client of the same pool is still not for us."""
        with pytest.raises(AuthError):
            auth.verify(mint(keys, client_id="some-other-app"))

    def test_an_id_token_is_refused_where_an_access_token_is_expected(
        self, auth: AuthConfig, keys: Keys
    ) -> None:
        with pytest.raises(AuthError):
            auth.verify(mint(keys, token_use="id", aud=AUDIENCE, client_id=None))

    def test_an_unknown_key_id_is_refused(self, auth: AuthConfig, keys: Keys) -> None:
        with pytest.raises(AuthError):
            auth.verify(mint(keys, kid="rotated-out-key"))

    def test_a_token_signed_by_another_key_is_refused(self, auth: AuthConfig) -> None:
        """The forger controls the payload but not the pool's private key."""
        attacker = _make_keys()  # same kid, different key
        with pytest.raises(AuthError):
            auth.verify(mint(attacker))

    def test_a_tampered_payload_is_refused(self, auth: AuthConfig, keys: Keys) -> None:
        header, _, rest = mint(keys).partition(".")
        payload, _, signature = rest.partition(".")
        forged = _b64({"iss": ISSUER, "sub": "somebody-else", "exp": int(time.time()) + 300})
        assert forged.decode() != payload
        with pytest.raises(AuthError):
            auth.verify(f"{header}.{forged.decode()}.{signature}")

    def test_an_unsigned_token_is_refused(self, auth: AuthConfig) -> None:
        """`alg: none` - the classic. Refused by the allowlist, before any key lookup."""
        header = _b64({"alg": "none", "typ": "JWT"})
        payload = _b64({"iss": ISSUER, "sub": "anyone", "exp": int(time.time()) + 300})
        with pytest.raises(AuthError):
            auth.verify(f"{header.decode()}.{payload.decode()}.")

    def test_the_public_key_is_not_accepted_as_an_hmac_secret(
        self, auth: AuthConfig, keys: Keys
    ) -> None:
        """The other classic: sign with HS256 using the published public key as the
        secret, and a verifier that trusts the header's `alg` accepts it.

        The token is assembled by hand because PyJWT refuses to *mint* this one, which
        would otherwise hide the fact that it also has to refuse it on the way in.
        """
        header = _b64({"alg": "HS256", "typ": "JWT", "kid": KID})
        payload = _b64({"iss": ISSUER, "sub": "anyone", "exp": int(time.time()) + 300})
        signing_input = header + b"." + payload
        signature = hmac.new(keys.public_pem.encode(), signing_input, hashlib.sha256).digest()
        tag = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        forged = f"{signing_input.decode()}.{tag}"
        with pytest.raises(AuthError):
            auth.verify(forged)

    def test_a_token_without_an_expiry_is_refused(self, auth: AuthConfig, keys: Keys) -> None:
        """A token that never expires cannot be revoked by waiting."""
        with pytest.raises(AuthError):
            auth.verify(mint(keys, exp=None))

    def test_garbage_is_refused(self, auth: AuthConfig) -> None:
        with pytest.raises(AuthError):
            auth.verify("not-a-token")

    def test_the_failure_reason_never_echoes_the_token(self, auth: AuthConfig, keys: Keys) -> None:
        """Error text reaches logs. A message quoting the token would put a credential
        there, which `docs/ARCHITECTURE_AND_ACCESS.md` forbids."""
        token = mint(keys, iss="https://accounts.example.com/other-pool")
        with pytest.raises(AuthError) as caught:
            auth.verify(token)
        message = str(caught.value)
        assert token not in message
        assert "accounts.example.com" not in message


class TestKeyRotationIsBounded:
    """Threshold support: an unknown key id refetches once, not once per request."""

    def test_an_unknown_kid_triggers_one_refetch_then_stops(self) -> None:
        keys = _make_keys()
        calls: list[str] = []

        def fetch(url: str) -> dict[str, Any]:
            calls.append(url)
            return keys.jwks

        clock = [1000.0]
        cache = CachedJwks(
            "https://example.invalid/jwks.json",
            fetch=fetch,
            min_refresh_seconds=60.0,
            clock=lambda: clock[0],
        )
        assert cache.key_for(KID) is not None
        assert len(calls) == 1  # cold

        for _ in range(20):
            with pytest.raises(AuthError):
                cache.key_for("unknown-kid")
        # Still one: a stream of forged key ids must not become a stream of outbound
        # requests to the identity provider.
        assert len(calls) == 1

        clock[0] += 61.0
        with pytest.raises(AuthError):
            cache.key_for("unknown-kid")
        assert len(calls) == 2  # rotation is picked up once the window has passed


# ---- 2. no privilege escalation -----------------------------------------


@pytest.fixture(scope="module")
def store() -> Iterator[Store]:
    """A clean set of tables per module, on a real PostgreSQL.

    On the fixture rather than the module, so the token tests above still run without a
    database - they are the larger half of this stage and need nothing but CPU.
    """
    if not TEST_URL:
        if os.getenv("CI"):
            raise RuntimeError(
                "CI must provide RSA_TEST_DATABASE_URL for the job running these tests. "
                "Skipping them means none of the M3 identity thresholds ran while CI "
                "still reports green."
            )
        pytest.skip("needs a real PostgreSQL: set RSA_TEST_DATABASE_URL (docker compose up -d db)")
    engine = make_engine(TEST_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield Store(make_session_factory(engine))
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(store: Store, auth: AuthConfig, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
    app = create_app(settings=Settings(), store=store, auth=auth)  # type: ignore[call-arg]
    with TestClient(app) as test_client:
        yield test_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_subject() -> str:
    return f"sub-{uuid.uuid4().hex[:12]}"


class TestRolesComeFromTheDatabase:
    """Threshold: roles asserted inside a token have no effect."""

    def test_a_token_claiming_supervisor_gets_only_what_the_database_granted(
        self, store: Store, auth: AuthConfig, keys: Keys
    ) -> None:
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        # The token asserts every role it can think of.
        token = auth.verify(
            mint(keys, subject=subject, extra={"cognito:groups": ["supervisor", "admin"]})
        )
        identity = store.identity_for(token.subject)
        assert identity is not None
        assert identity.roles == frozenset({"customer"})
        assert not identity.may(Permission.AUDIT_READ)

    def test_the_audit_route_refuses_a_customer_asserting_supervisor(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        token = mint(keys, subject=subject, extra={"cognito:groups": ["supervisor"]})
        response = client.get(f"/session/{new_subject()}/audit", headers=bearer(token))
        assert response.status_code == 403

    def test_a_supervisor_may_read_the_audit_trail(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        """The positive control. Without it, a 403 everywhere would also pass the test
        above while proving nothing."""
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["supervisor"])
        response = client.get(
            f"/session/{new_subject()}/audit", headers=bearer(mint(keys, subject=subject))
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_revoking_a_role_takes_effect_on_the_next_request(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        """The reason roles are not read from the token: a claim inside an issued token
        cannot be withdrawn, and this one is."""
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["supervisor"])
        token = mint(keys, subject=subject)
        path = f"/session/{new_subject()}/audit"
        assert client.get(path, headers=bearer(token)).status_code == 200

        store.revoke_role(subject, "supervisor")
        # The same token, still valid, still signed, now allowed nothing.
        assert client.get(path, headers=bearer(token)).status_code == 403

    def test_a_verified_token_for_an_unprovisioned_subject_is_refused(
        self, client: TestClient, keys: Keys
    ) -> None:
        """Authenticated is not authorized. Nobody gets an account by showing up."""
        response = client.get(
            f"/session/{new_subject()}/audit", headers=bearer(mint(keys, subject=new_subject()))
        )
        assert response.status_code == 403

    def test_an_unknown_role_name_grants_nothing(self) -> None:
        assert permissions_for(["not-a-role"]) == frozenset()


class TestWhatTheCallerIsToldAboutThemselves:
    """`/me` is what the demo page renders, so it is where "the identity is not
    selectable" becomes something a person can see rather than read about."""

    def test_it_reports_the_roles_the_database_granted_not_the_ones_asserted(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        token = mint(keys, subject=subject, extra={"cognito:groups": ["supervisor"]})

        body = client.get("/me", headers=bearer(token)).json()
        assert body["roles"] == ["customer"]
        assert "audit:read" not in body["permissions"]

    def test_it_reports_the_retail_identity_the_database_maps(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        """The demo renders this. A page that showed a name the request had chosen would
        make the whole claim untestable by eye."""
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        body = client.get("/me", headers=bearer(mint(keys, subject=subject))).json()
        assert body["user_id"] == RETAIL_USER

    def test_it_does_not_return_the_subject(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        """It identifies the account in the identity provider, and no part of the page
        needs it."""
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        body = client.get("/me", headers=bearer(mint(keys, subject=subject))).json()
        assert subject not in json.dumps(body)

    def test_an_unprovisioned_subject_is_refused_here_too(
        self, client: TestClient, keys: Keys
    ) -> None:
        """The UI calls this at sign-in, so this refusal is what turns "authenticated and
        authorized to do nothing" into a message rather than an empty conversation."""
        response = client.get("/me", headers=bearer(mint(keys, subject=new_subject())))
        assert response.status_code == 403


class TestTheApiRefusesUnauthenticatedRequests:
    """Threshold: with authentication on, no route serves an anonymous caller."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", "/chat", {"session_id": "s1", "message": "hello"}),
            # The streaming route refuses the same way, and it has to refuse before the
            # response begins: once the first event is out there is no status code left
            # to send (M7.1).
            ("post", "/chat/stream", {"session_id": "s1", "message": "hello"}),
            ("get", "/me", None),
            ("get", "/session/s1/audit", None),
            ("post", "/session/s1/reset", None),
            ("post", "/sessions/reset", None),
        ],
    )
    def test_every_route_requires_a_token(
        self, client: TestClient, method: str, path: str, body: dict[str, Any] | None
    ) -> None:
        response = client.request(method.upper(), path, json=body)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_an_expired_token_is_401_not_403(self, client: TestClient, keys: Keys) -> None:
        """401 and 403 answer different questions, and a caller acts on the difference:
        401 means get a new token, 403 means stop asking."""
        now = int(time.time())
        token = mint(keys, iat=now - 7200, exp=now - 3600)
        assert client.post("/sessions/reset", headers=bearer(token)).status_code == 401

    def test_the_idle_route_stays_public_because_the_machine_calls_it(
        self, client: TestClient
    ) -> None:
        """The watcher runs on the instance and has no token. It reads counts, not data,
        and requiring one would mean putting a credential on the machine to shut the
        machine down."""
        assert client.get("/idle").status_code == 200

    def test_health_stays_public_and_names_the_mode(self, client: TestClient) -> None:
        """An operator must be able to see whether authentication is on without holding
        a token. Nothing about a user is exposed there."""
        body = client.get("/health").json()
        assert body["auth"] == "required"
        assert "demo_user_id" not in body


# ---- 3. session ownership -----------------------------------------------


class TestSessionOwnership:
    """Threshold: a verified caller cannot reach another subject's session."""

    def _session_of(self, store: Store, subject: str) -> str:
        session_id = f"s-{uuid.uuid4().hex[:12]}"
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        store.ensure_session(session_id, RETAIL_USER, subject)
        return session_id

    def test_another_subject_cannot_continue_the_conversation(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        owner, intruder = new_subject(), new_subject()
        session_id = self._session_of(store, owner)
        store.provision_identity(intruder, RETAIL_USER, ["customer"])

        response = client.post(
            "/chat",
            json={"session_id": session_id, "message": "what did I just confirm?"},
            headers=bearer(mint(keys, subject=intruder)),
        )
        # 403 before the model is ever called: refusing early is what keeps another
        # user's conversation out of a reply.
        assert response.status_code == 403
        assert "another account" in response.json()["detail"]

    def test_another_subject_cannot_reset_the_session(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        owner, intruder = new_subject(), new_subject()
        session_id = self._session_of(store, owner)
        store.provision_identity(intruder, RETAIL_USER, ["customer"])
        response = client.post(
            f"/session/{session_id}/reset", headers=bearer(mint(keys, subject=intruder))
        )
        assert response.status_code == 403

    def test_the_owner_may_reset_it(self, client: TestClient, store: Store, keys: Keys) -> None:
        owner = new_subject()
        session_id = self._session_of(store, owner)
        response = client.post(
            f"/session/{session_id}/reset", headers=bearer(mint(keys, subject=owner))
        )
        assert response.status_code == 200

    def test_an_unknown_session_is_not_owned_by_anyone(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        """Resetting something that was never created stays a cheap no-op rather than
        becoming a 403 that leaks whether a session id exists."""
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        response = client.post(
            "/session/never-existed/reset", headers=bearer(mint(keys, subject=subject))
        )
        assert response.status_code == 200

    def test_the_session_records_the_subject_that_opened_it(self, store: Store) -> None:
        subject = new_subject()
        session_id = f"s-{uuid.uuid4().hex[:12]}"
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        assert store.ensure_session(session_id, RETAIL_USER, subject) == subject
        # A second caller cannot take it over by asking again with their own subject.
        assert store.ensure_session(session_id, RETAIL_USER, "someone-else") == subject
        assert store.session_subject(session_id) == (True, subject)

    def test_resetting_all_sessions_needs_its_own_permission(
        self, client: TestClient, store: Store, keys: Keys
    ) -> None:
        """The one route that crosses session boundaries. A customer holds every
        permission they need for their own data and still may not use it."""
        customer, supervisor = new_subject(), new_subject()
        store.provision_identity(customer, RETAIL_USER, ["customer"])
        store.provision_identity(supervisor, RETAIL_USER, ["supervisor"])
        assert (
            client.post("/sessions/reset", headers=bearer(mint(keys, subject=customer))).status_code
            == 403
        )
        assert (
            client.post(
                "/sessions/reset", headers=bearer(mint(keys, subject=supervisor))
            ).status_code
            == 200
        )


# ---- 4. resource ownership and role enforcement -------------------------


def owner_lookup(order_id: str) -> str | None:
    """Stand-in for RetailDomain.order_owner. The guard's ownership rule is what is
    under test here, not the domain's storage."""
    return {"#W001": RETAIL_USER, "#W999": "someone_else_5150"}.get(order_id)


CANCEL_OWN = ProposedCall(
    "cancel_pending_order", {"order_id": "#W001", "reason": "no longer needed"}
)
CANCEL_OTHER = ProposedCall(
    "cancel_pending_order", {"order_id": "#W999", "reason": "no longer needed"}
)


def guard() -> WriteGuard:
    return WriteGuard({"cancel_pending_order", "get_order_details"}, owner_lookup)


class TestResourceOwnership:
    """Threshold: the identity the ownership check uses is the authenticated one."""

    def test_a_write_against_another_users_order_is_denied(self) -> None:
        identity = Identity(subject="sub-1", user_id=RETAIL_USER, roles=frozenset({"customer"}))
        state = SessionState(user_id=identity.user_id, permissions=identity.permissions)
        state.confirm(CANCEL_OTHER)  # even confirmed, and even confirmed by the owner
        verdict = guard().review(CANCEL_OTHER, state)
        assert verdict.decision is Decision.DENIED_OWNERSHIP

    def test_a_write_against_their_own_order_still_works(self) -> None:
        """The M0.3 behaviour must survive M3: authorization was added, not tightened
        into refusing everything."""
        identity = Identity(subject="sub-1", user_id=RETAIL_USER, roles=frozenset({"customer"}))
        state = SessionState(user_id=identity.user_id, permissions=identity.permissions)
        state.confirm(CANCEL_OWN)
        assert guard().review(CANCEL_OWN, state).allowed

    def test_the_request_body_cannot_name_a_user(self) -> None:
        """The retail identity is derived from the token's subject through `identities`.
        A field here would be a way to ask for someone else's data, so there is none."""
        assert set(ChatRequest.model_fields) == {"session_id", "message"}

    def test_the_identity_carries_the_user_id_the_database_holds(self, store: Store) -> None:
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        identity = store.identity_for(subject)
        assert identity is not None and identity.user_id == RETAIL_USER


class TestRoleEnforcement:
    """Threshold: a role without the permission is refused, however well authorized."""

    def test_a_role_without_the_write_permission_cannot_write(self) -> None:
        auditor = Identity(subject="sub-2", user_id=RETAIL_USER, roles=frozenset({"auditor"}))
        state = SessionState(user_id=auditor.user_id, permissions=auditor.permissions)
        state.confirm(CANCEL_OWN)  # owns the order, and has confirmed the exact arguments
        verdict = guard().review(CANCEL_OWN, state)
        assert verdict.decision is Decision.DENIED_PERMISSION
        assert "confirmation does not grant a permission" in verdict.reason

    def test_a_role_without_the_read_permission_cannot_read(self) -> None:
        nobody = Identity(subject="sub-3", user_id=RETAIL_USER, roles=frozenset())
        state = SessionState(user_id=nobody.user_id, permissions=nobody.permissions)
        read = ProposedCall("get_order_details", {"order_id": "#W001"})
        assert guard().review(read, state).decision is Decision.DENIED_PERMISSION

    def test_permission_is_checked_before_ownership(self) -> None:
        """A refusal must not tell an account with no write permission which orders
        exist, so the weaker check answers first."""
        auditor = Identity(subject="sub-2", user_id=RETAIL_USER, roles=frozenset({"auditor"}))
        state = SessionState(user_id=auditor.user_id, permissions=auditor.permissions)
        verdict = guard().review(CANCEL_OTHER, state)
        assert verdict.decision is Decision.DENIED_PERMISSION

    def test_require_raises_for_a_missing_permission(self) -> None:
        customer = Identity(subject="sub-4", user_id=RETAIL_USER, roles=frozenset({"customer"}))
        customer.require(Permission.ORDER_WRITE)  # holds it
        with pytest.raises(NotAuthorizedError):
            customer.require(Permission.AUDIT_READ)


# ---- 5. cost ------------------------------------------------------------


class TestAuthenticationCost:
    """Threshold: the per-request cost is measured and reported, not assumed."""

    def test_verification_cost(self, auth: AuthConfig, keys: Keys) -> None:
        token = mint(keys)
        auth.verify(token)  # warm any lazy key parsing
        samples = []
        for _ in range(200):
            start = time.perf_counter()
            auth.verify(token)
            samples.append((time.perf_counter() - start) * 1000)
        samples.sort()
        p50, p95 = median(samples), samples[int(len(samples) * 0.95)]
        print(f"\ntoken verification: median {p50:.3f} ms, p95 {p95:.3f} ms")
        # An RS256 verification is tens of microseconds of arithmetic. A bound this
        # loose only catches a change of kind - a network call, or a key parsed per
        # request - which is what would actually matter here.
        assert p95 < 20.0

    def test_the_key_set_is_fetched_once_not_per_request(self, keys: Keys) -> None:
        calls: list[str] = []

        def fetch(url: str) -> dict[str, Any]:
            calls.append(url)
            return keys.jwks

        config = AuthConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            keys=CachedJwks("https://example.invalid/jwks.json", fetch=fetch),
        )
        token = mint(keys)
        for _ in range(50):
            config.verify(token)
        assert len(calls) == 1

    def test_identity_lookup_cost(self, store: Store, keys: Keys) -> None:
        subject = new_subject()
        store.provision_identity(subject, RETAIL_USER, ["customer"])
        samples = []
        for _ in range(50):
            start = time.perf_counter()
            store.identity_for(subject)
            samples.append((time.perf_counter() - start) * 1000)
        samples.sort()
        print(
            f"\nidentity lookup: median {median(samples):.3f} ms, "
            f"p95 {samples[int(len(samples) * 0.95)]:.3f} ms"
        )
        # Two indexed reads against a loopback PostgreSQL. The number is the point; the
        # bound only catches a regression of kind, such as a missing index.
        assert median(samples) < 50.0


# ---- 6. degradation ------------------------------------------------------


class TestWithoutAuthenticationConfigured:
    """Threshold: with no issuer configured, M0/M1/M2 behaviour is unchanged."""

    @pytest.fixture
    def open_client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        return TestClient(create_app(settings=Settings()))  # type: ignore[call-arg]

    def test_no_token_is_needed(self, open_client: TestClient) -> None:
        assert open_client.post("/session/s1/reset").status_code == 200
        assert open_client.post("/sessions/reset").status_code == 200

    def test_health_says_authentication_is_disabled(self, open_client: TestClient) -> None:
        body = open_client.get("/health").json()
        assert body["auth"] == "disabled"
        assert body["demo_user_id"] == DEMO_USER_ID

    def test_the_demo_identity_can_operate_on_its_own_orders(self) -> None:
        """The evaluation loop and the demo run as a customer, so the default session
        state must keep the M0.3 permissions."""
        state = SessionState(user_id=RETAIL_USER)
        state.confirm(CANCEL_OWN)
        assert guard().review(CANCEL_OWN, state).allowed

    def test_settings_report_authentication_off_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert not settings.has_auth()
        assert settings.jwks_url() is None


class TestConfigurationIsRefusedWhenIncoherent:
    """A service that cannot serve anyone should say so at startup, not per request."""

    def test_authentication_without_a_database_fails_at_startup(
        self, auth: AuthConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        monkeypatch.delenv("RSA_DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="RSA_DATABASE_URL"):
            create_app(settings=Settings(_env_file=None), auth=auth)  # type: ignore[call-arg]

    def test_an_issuer_without_an_audience_is_not_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Half a configuration would accept any token from the pool, including one
        minted for a different application."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        monkeypatch.setenv("RSA_AUTH_ISSUER", ISSUER)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert not settings.has_auth()

    def test_the_key_set_url_is_derived_from_the_issuer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        monkeypatch.setenv("RSA_AUTH_ISSUER", ISSUER)
        monkeypatch.setenv("RSA_AUTH_AUDIENCE", AUDIENCE)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.has_auth()
        assert settings.jwks_url() == f"{ISSUER}/.well-known/jwks.json"

    def test_a_plain_http_key_set_is_refused(self) -> None:
        """An http:// key set can be replaced in transit, and every key in it would then
        be attacker-chosen."""
        cache = CachedJwks("http://example.invalid/jwks.json")
        with pytest.raises(AuthError):
            cache.key_for(KID)


# ---- 7. the administrative interface ------------------------------------


class TestProvisioningScript:
    """`scripts/provision_identity.py` is the only way an account comes into being, so
    it is tested like a route rather than treated as a convenience."""

    @pytest.fixture(autouse=True)
    def _database(self, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
        assert TEST_URL
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-key-not-real-0000000000")
        monkeypatch.setenv("RSA_DATABASE_URL", TEST_URL)

    def run(self, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
        monkeypatch.setattr(sys, "argv", ["provision_identity.py", *args])
        return provision_main()

    def test_granting_then_revoking_a_role(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subject = new_subject()
        assert (
            self.run(
                monkeypatch, "--subject", subject, "--user-id", RETAIL_USER, "--role", "supervisor"
            )
            == 0
        )
        identity = store.identity_for(subject)
        assert identity is not None and identity.may(Permission.AUDIT_READ)

        assert self.run(monkeypatch, "--subject", subject, "--revoke", "supervisor") == 0
        identity = store.identity_for(subject)
        assert identity is not None and not identity.may(Permission.AUDIT_READ)

    def test_a_revocation_is_applied_before_the_state_is_printed(
        self, store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """It printed the state it had just been asked to change, which is the one state
        an operator running --revoke --show must not be shown."""
        subject = new_subject()
        self.run(monkeypatch, "--subject", subject, "--user-id", RETAIL_USER, "--role", "customer")
        capsys.readouterr()
        self.run(monkeypatch, "--subject", subject, "--revoke", "customer", "--show")
        assert "roles: (none)" in capsys.readouterr().out

    def test_a_new_subject_needs_a_retail_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A role granted to a subject with no retail identity would authorize actions on
        nobody's orders."""
        with pytest.raises(SystemExit):
            self.run(monkeypatch, "--subject", new_subject(), "--role", "customer")

    def test_showing_an_unknown_subject_reports_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self.run(monkeypatch, "--subject", new_subject(), "--show") == 1
