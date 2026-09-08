"""Authentication and authorization: verified tokens in, permissions out.

Two rules carry this module:

1. The token establishes *who*, never *what they may do*. A Cognito access token
   carries `cognito:groups`, and those claims are deliberately ignored: a group claim
   is only as trustworthy as every path that can write a group, and a token minted
   before a role was revoked stays valid until it expires. Roles are read from the
   database (`role_memberships`), so revoking one takes effect on the next request.
2. Verification is called with an explicit algorithm allowlist and explicit required
   claims. The two classic failures - `alg: none`, and an RS256 public key accepted as
   an HMAC secret - are refused by configuration rather than by hoping the defaults are
   safe.

No token is ever issued here, and no token, key or claim value is logged. `AuthError`
messages say what kind of check failed, never what the token contained.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import jwt
from jwt import PyJWK, PyJWKSet
from jwt.exceptions import PyJWTError

# RS256 only. Cognito signs with RS256, and an allowlist of one leaves no room for a
# downgrade: `none` is absent, and so is every HMAC algorithm, which is what stops a
# forged token signed with the public key from being accepted.
ALLOWED_ALGORITHMS = ("RS256",)

# Claims that must be present. `exp` is required explicitly: PyJWT verifies an
# expiry it finds, but a token without one would otherwise pass and never expire.
REQUIRED_CLAIMS = ("exp", "iat", "iss", "sub")


class AuthError(Exception):
    """The caller is not authenticated. Always answered with 401.

    The message describes the failed check, never the token's content: an error string
    that echoes a claim ends up in a log, and this module must not put credentials there.
    """


class NotAuthorizedError(Exception):
    """The caller is authenticated but not allowed. Always answered with 403.

    Not named `PermissionError`: that name is a builtin whose subclass is OSError, and
    shadowing it in a module about authorization would be a genuine trap at any call
    site that catches OSError.
    """


class Permission(StrEnum):
    """What an account may do. Checked on the backend, never only in the tool list."""

    ORDER_READ = "order:read"
    ORDER_WRITE = "order:write"
    AUDIT_READ = "audit:read"
    SESSION_RESET_ALL = "session:reset_all"


# The role matrix from docs/ARCHITECTURE_AND_ACCESS.md, restricted to what this stage
# can actually enforce.
#
# `agent` gains nothing over `customer` here, and that is honest rather than an
# oversight: an agent acting on another customer's order requires a ticket assignment,
# which is M4. Granting a permission the code cannot honour would make the matrix
# decorative.
ROLE_PERMISSIONS: Mapping[str, frozenset[Permission]] = {
    "customer": frozenset({Permission.ORDER_READ, Permission.ORDER_WRITE}),
    "agent": frozenset({Permission.ORDER_READ, Permission.ORDER_WRITE}),
    "supervisor": frozenset(
        {
            Permission.ORDER_READ,
            Permission.ORDER_WRITE,
            Permission.AUDIT_READ,
            Permission.SESSION_RESET_ALL,
        }
    ),
    # Read-only access to the audit trail and nothing else. It exists to make the
    # intersection rule testable: a role can hold a permission without holding the
    # write, and holding the write is what the guard checks.
    "auditor": frozenset({Permission.AUDIT_READ}),
    # Only for the identity the service invents when authentication is not configured:
    # the local demo has one user who must also be able to reset the demo. It is listed
    # here rather than special-cased in the API so that every permission any request can
    # hold comes from one table.
    "demo": frozenset(
        {Permission.ORDER_READ, Permission.ORDER_WRITE, Permission.SESSION_RESET_ALL}
    ),
}

# What a `SessionState` gets when nobody says otherwise: a customer acting on their own
# orders, which is the M0 demo and evaluation behaviour.
DEFAULT_PERMISSIONS: frozenset[Permission] = ROLE_PERMISSIONS["customer"]


def permissions_for(roles: Iterable[str]) -> frozenset[Permission]:
    """Union of the roles' permissions. An unknown role contributes nothing.

    Unknown roles are ignored rather than rejected because a role can be added to the
    database by a later stage before this table knows about it, and the safe reading of
    a name with no definition is "no permissions", not "all of them".
    """
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


@dataclass(frozen=True)
class Identity:
    """An authenticated caller, after the database has said what they may do.

    `subject` is the token's `sub`, stable for the life of the account. `user_id` is the
    retail identity that owns orders - derived from `identities`, never from the request,
    so a client cannot select whose data it operates on.
    """

    subject: str
    user_id: str
    roles: frozenset[str] = frozenset()

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.roles)

    def may(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.may(permission):
            raise NotAuthorizedError(f"This account does not have {permission.value}.")


@dataclass(frozen=True)
class VerifiedToken:
    """A token whose signature and claims passed. `claims` is what it asserted.

    Callers read `subject` and nothing else for authorization; `claims` is kept for
    diagnostics and for the audit record of which issuer authenticated the request.
    """

    subject: str
    issuer: str
    claims: Mapping[str, Any]


class KeySource(Protocol):
    """Where public keys come from. Injected so tests need no network."""

    def key_for(self, kid: str | None) -> PyJWK: ...


class StaticKeys:
    """A fixed JWKS document. Used by tests and by an offline deployment."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        self._keys = PyJWKSet.from_dict(dict(jwks))

    def key_for(self, kid: str | None) -> PyJWK:
        for key in self._keys.keys:
            if kid is None or key.key_id == kid:
                return key
        raise AuthError("The token was signed with an unknown key.")


class CachedJwks:
    """JWKS fetched over HTTPS and cached, with a bounded refresh on an unknown key id.

    Two behaviours matter and neither is the default anywhere:

    - A cache with no refresh breaks on the provider's next key rotation, so an unknown
      `kid` triggers one refetch.
    - A refetch on *every* unknown `kid` turns a stream of forged tokens into a stream
      of outbound requests, which is a denial-of-service amplifier. `min_refresh_seconds`
      bounds it.
    """

    def __init__(
        self,
        url: str,
        fetch: Callable[[str], Mapping[str, Any]] | None = None,
        ttl_seconds: float = 3600.0,
        min_refresh_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._fetch = fetch or _fetch_jwks
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._clock = clock
        self._keys: PyJWKSet | None = None
        self._fetched_at = 0.0

    def key_for(self, kid: str | None) -> PyJWK:
        now = self._clock()
        if self._keys is None or now - self._fetched_at >= self._ttl:
            self._load()
        key = self._lookup(kid)
        if key is None and self._clock() - self._fetched_at >= self._min_refresh:
            self._load()
            key = self._lookup(kid)
        if key is None:
            raise AuthError("The token was signed with an unknown key.")
        return key

    def _lookup(self, kid: str | None) -> PyJWK | None:
        if self._keys is None:
            return None
        for key in self._keys.keys:
            if kid is None or key.key_id == kid:
                return key
        return None

    def _load(self) -> None:
        try:
            document = self._fetch(self._url)
        except Exception as exc:  # network, DNS, malformed JSON
            # The URL is configuration, not a secret, but the exception text can carry
            # a redirect target, so only the type is reported.
            raise AuthError(f"The key set could not be fetched ({type(exc).__name__}).") from exc
        self._keys = PyJWKSet.from_dict(dict(document))
        self._fetched_at = self._clock()


def _fetch_jwks(url: str) -> Mapping[str, Any]:
    """Fetch a JWKS document. HTTPS only: an http:// key set can be replaced in transit,
    and every key in it would then be attacker-chosen."""
    if not url.startswith("https://"):
        raise ValueError("The key set URL must be https.")
    # The scheme is checked above, so this cannot open a file:// or local URL.
    with urllib.request.urlopen(url, timeout=5) as response:
        payload: Mapping[str, Any] = json.loads(response.read().decode("utf-8"))
    return payload


@dataclass(frozen=True)
class AuthConfig:
    """What a token must prove. Every field is checked; none has a permissive default.

    `audience` is compared against `aud` when present and `client_id` otherwise, because
    a Cognito access token carries `client_id` while an id token carries `aud`. Both are
    the same question - which application this token was minted for - and accepting a
    token minted for a different application is the wrong-audience failure.
    """

    issuer: str
    audience: str
    keys: KeySource
    token_use: str | None = "access"
    # Small clock skew between the issuer and this service. Bounded and explicit: a
    # large leeway is an expired token being accepted.
    leeway_seconds: float = 30.0

    def verify(self, token: str) -> VerifiedToken:
        """Verify a bearer token, or raise AuthError.

        Every failure raises the same exception type with a short reason. The reason
        distinguishes the failed check for the operator; it never echoes the token.
        """
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTError as exc:
            raise AuthError("The token is malformed.") from exc

        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            # This is where `alg: none` and `alg: HS256` die. Passing `algorithms=`
            # to decode() would also refuse them; checking here as well means the
            # refusal does not depend on one library call's argument staying correct.
            raise AuthError(f"Unsupported signing algorithm: {algorithm!r}.")

        key = self.keys.key_for(header.get("kid"))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                # Audience is checked below against aud or client_id, so PyJWT's own
                # audience check is turned off rather than given a value it may not find.
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": False,
                    "require": list(REQUIRED_CLAIMS),
                },
            )
        except PyJWTError as exc:
            raise AuthError(f"The token failed verification ({type(exc).__name__}).") from exc

        self._check_audience(claims)
        if self.token_use is not None and claims.get("token_use") != self.token_use:
            raise AuthError("The token is not of the expected type.")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthError("The token has no usable subject.")
        return VerifiedToken(subject=subject, issuer=str(claims["iss"]), claims=claims)

    def _check_audience(self, claims: Mapping[str, Any]) -> None:
        audience = claims.get("aud", claims.get("client_id"))
        accepted = audience if isinstance(audience, list) else [audience]
        if self.audience not in accepted:
            raise AuthError("The token was issued for another application.")
