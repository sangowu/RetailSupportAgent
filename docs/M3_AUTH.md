# M3: authentication, roles and resource permissions

## 0. Why this step exists

Everything up to M2 runs under one hard-coded identity. `create_app()` takes
`user_id=DEMO_USER_ID`, every request is that user, and the only thing keeping another
person out is that the service binds to loopback. Three consequences:

1. **Nothing proves who is calling.** `SessionState.user_id` is a constant, so the
   ownership check in `WriteGuard._check_ownership` compares a resource owner against a
   value the server invented rather than against an authenticated subject. The check is
   real code exercised by tests, but the identity it defends is not established.
2. **A session belongs to no one.** `/chat` takes a `session_id` string. Any caller who
   knows or guesses one can continue that conversation, read the replies and confirm a
   pending write. M2 added a claim, but a claim answers "which process", not "which
   person": a second caller with the same process gets the session.
3. **There are no roles.** `docs/ARCHITECTURE_AND_ACCESS.md` states a three-role matrix
   and the rule that the tool set is the intersection of what the account is authorized
   to do and what the task requires. None of it exists in code, so "the model cannot widen
   its own authorization" is currently true only because there is nothing to widen.

M3 solves those three and nothing else.

## 1. Change card

- **Stage and step**: M3
- **Type**: feature
- **The specific problem**: the service has no authentication, no session ownership and
  no roles, so every authorization decision rests on a constant chosen by the server
- **The single theme of this change**: establish the caller's identity from a verified
  token, derive what they may do from the database, and enforce both at the HTTP boundary
  and on every tool call
- **Control variables this change does not touch**: the model under test, the simulated
  user, the judge, the task sets, the upstream commit, the price snapshot, the M1 schema's
  existing semantics and the M2 claim. The evaluation stack is untouched, or the
  comparison against `baseline-v3` means nothing.
- **Explicitly out of scope**:
  - **No AWS.** M3 implements and tests the verifier for a Cognito-shaped token against a
    locally generated key pair. Pointing it at a real user pool is configuration, and it
    happens in M6 where AWS is actually provisioned. What is verified here is the
    mechanism, not a deployment - the same boundary M2 drew around its lease.
  - **No registration, no password handling, no token issuing.** The service verifies
    tokens; it never mints one outside tests. An identity provider is not being rebuilt.
  - **No tickets, assignments, approval or handoff.** Those are M4. An `agent` role
    therefore gains nothing over a `customer` in M3, because acting on another customer's
    order requires an assignment that does not exist yet. That is stated rather than
    papered over with a permission the code cannot honour.
  - **The τ²-bench simulated order data still stays out of PostgreSQL**, for the reason
    M1 gave.
- **Expected benefit**: an unauthenticated request is refused; a forged or expired token
  is refused; one user cannot reach another user's session or orders; a role that lacks a
  permission is refused even when it owns the resource and has confirmed the action
- **Possible cost**: one signature verification per request, plus one database read for
  the identity and its roles; a JWKS fetch on a cache miss

### 1.1 Design decisions fixed before the work

**Roles come from the database, never from the token.** A Cognito access token carries
`cognito:groups`, and it is tempting to read authorization straight out of it. This
implementation ignores those claims. The token establishes *who* (`sub`, signed by the
issuer); the `role_memberships` table decides *what they may do*. Two reasons: a group
claim is only as trustworthy as every path that can write a group, and a token minted
before a role was revoked stays valid until it expires. This is what makes the "no
privilege escalation" threshold testable - a token that claims `supervisor` gets exactly
the permissions the database grants it, which the test asserts is none.

**The retail identity is derived, not submitted.** The `user_id` the guard checks
ownership against comes from `identities.user_id` for the verified subject. No request
body field can influence it. The client cannot select a tenant, per the architecture
document.

**Session ownership is a separate check from the M2 claim.** The claim answers "which
process is driving this session" and refuses with 409. Ownership answers "whose session is
this" and refuses with 403. They are different failures and must not share a status code:
409 invites the caller to retry elsewhere, 403 tells them to stop.

**Authentication is off until it is configured.** With no issuer, audience and key source
set, the service keeps the M0 fixed-identity, loopback-only behaviour, because the
evaluation loop and the local demo do not go through an identity provider. `/health`
reports which mode is live so the state is never ambiguous. When it is configured, every
endpoint requires a valid bearer token; there is no per-route opt-out.

**The signature check uses PyJWT, not hand-written code.** The verification itself is
called with an explicit algorithm allowlist and required claims rather than defaults, so
the two classic failures - `alg: none` and an RS256 public key accepted as an HMAC secret
- are refused by configuration, and both have a test.

### 1.2 Primary metric, protective metrics, acceptance thresholds

Fixed before running, per `MODEL_EXPERIMENTS.md`.

| Metric | Threshold | If not met |
|---|---|---|
| Token verification | Missing, malformed, expired, wrong issuer, wrong audience, wrong `token_use`, unknown `kid`, tampered payload, `alg: none` and HS256-with-the-public-key are each refused with 401 | FAIL |
| No privilege escalation | Roles asserted inside the token have no effect; permissions equal exactly what `role_memberships` grants | FAIL |
| Session ownership | A verified caller cannot read or continue another subject's session; refused with 403 | FAIL |
| Resource ownership | A write against an order owned by another user is denied, with the identity now coming from the token rather than a constant | FAIL |
| Role enforcement | A role without the write permission is denied a write it owns and has confirmed | FAIL |
| Authentication overhead | Per-request verification cost measured and reported, with the key cache warm and cold | Explain the cause |
| Degradation | With authentication unconfigured, behaviour is M0/M1/M2 unchanged and the whole existing suite passes | FAIL |
| Evaluation score | No regression against `baseline-v3` | Investigate before proceeding |

### 1.3 Baseline and evaluation protocol

- Baseline: `baseline-v3` (this project's agent, 114 tasks, 102/114), protocol
  `evaluation/protocol.md` at the pinned upstream commit
  `fc0055dc4e0a316c3f83133267fbd6faaa770992`.
- The evaluation loop drives `bench_agent.py` directly and never constructs the FastAPI
  application, so the authentication path does not participate in a benchmark run. The
  expected result for the last row is therefore "not applicable", which section 3 must
  either confirm by inspection or replace with a measured number - not assume.

## 2. Implementation

| File | Responsibility |
|---|---|
| `src/retail_support_agent/auth.py` | Token verification, the role-to-permission table, `Identity` |
| `src/retail_support_agent/db.py` | `identities`, `role_memberships`, `sessions.subject` |
| `migrations/versions/0003_identities.py` | The migration for all three, with a working `downgrade` |
| `src/retail_support_agent/store.py` | `provision_identity` / `revoke_role` / `identity_for`, session ownership on `ensure_session` and `session_subject` |
| `src/retail_support_agent/api.py` | The bearer dependency, 401/403, per-route permissions, the audit route |
| `src/retail_support_agent/guardrails.py` | `Permission` checks before ownership and confirmation |
| `scripts/provision_identity.py` | The whole administrative interface: grant, revoke, show |
| `tests/test_auth.py` | 55 tests: one class per acceptance threshold, plus the admin script |

A request now passes four checks in order, each stricter than the last: the token
verifies (401), the subject has an account (403), the account holds the permission the
route needs (403), and the session belongs to it (403). Only then does the turn run, and
the guard applies the same permission set again per tool call.

### 2.1 What the token is allowed to decide

Exactly one thing: the value of `sub`. Everything else comes from the database.

```text
token  ->  sub  ->  identities.user_id     (whose orders)
                ->  role_memberships.role  (what may be done)
```

`cognito:groups` is read by nothing. The test that pins this mints a token asserting
`["supervisor", "admin"]` for a subject the database grants `customer`, and checks the
resulting identity holds the customer permissions and is refused the audit route.

### 2.2 Why the verifier is exercised without AWS

The tests generate an RSA key pair, publish its public half as a JWKS document and mint
real RS256 tokens. That covers signature, issuer, audience, expiry, token type, key id,
`alg: none` and the HS256-with-the-public-key forgery against the same code path a
Cognito token takes. What is not covered is the pool itself - key rotation as AWS
performs it, and the network fetch - and that belongs with M6, where the pool exists.

## 3. Results

Verified against PostgreSQL 17 (`compose.yaml`, loopback only) with
`RSA_TEST_DATABASE_URL` set. All 55 new tests executed rather than skipped; the whole
suite is 386 tests passing, against 331 on `main` before this change. The tests needing a
database refuse to skip under `CI`, for the reason M1 gave: a silent skip reads exactly
like a pass in the summary line.

### 3.1 The acceptance thresholds

| Metric | Threshold | Measured | Result |
|---|---|---|---|
| Token verification | Missing, malformed, expired, wrong issuer, wrong audience, wrong `token_use`, unknown `kid`, tampered payload, `alg: none`, HS256-with-the-public-key each refused with 401 | 10 refusal cases plus a valid-token control; the four API routes each answer 401 with `WWW-Authenticate: Bearer` | pass |
| No privilege escalation | Token-asserted roles have no effect | a token claiming `supervisor` and `admin` gets the customer permissions and 403 on the audit route; revoking a role takes effect on the next request with the same token | pass |
| Session ownership | Another subject is refused with 403 | 403 on `/chat` and on reset, before the model is called; the owner still passes | pass |
| Resource ownership | A write on another user's order is denied with the identity from the token | denied even when confirmed | pass |
| Role enforcement | A role without the permission is refused | `auditor` denied a write it owns and has confirmed, and denied a read | pass |
| Authentication overhead | Measured and reported | verification median 0.035 ms; the database reads dominate at about 6.5 ms per turn (3.4) | pass |
| Degradation | Unconfigured behaves as M0/M1/M2 | no token needed, `/health` says `disabled`, the previous 331 tests still pass | pass |
| Evaluation score | No regression against `baseline-v3` | not applicable; see 3.5 | see 3.5 |

### 3.2 The two forgeries that get verifiers wrong

Both have a test, and both are refused before any key is loaded, because the algorithm
allowlist is checked against the header first:

- **`alg: none`.** A token with an empty signature, which a verifier that trusts the
  header accepts as valid.
- **HS256 signed with the pool's public key.** The public key is published; a verifier
  that picks its algorithm from the header treats it as an HMAC secret and every token
  becomes forgeable. PyJWT refuses to *mint* this one, so the test assembles it by hand -
  otherwise the case would appear covered while never having been constructed.

`decode()` is also given `algorithms=["RS256"]`, so the refusal does not depend on one
check staying correct.

### 3.3 Why roles are not read from the token

A group claim is trustworthy only as far as every path that can write a group, and an
issued token keeps asserting what it said until it expires. The test for this revokes a
supervisor role and reuses the same still-valid token: the request that succeeded a
moment earlier is now 403. With roles read from the claim, revocation would have taken
effect whenever the token happened to expire.

The cost is one indexed read per request, measured below.

### 3.4 What authentication costs

| Operation | Measured |
|---|---|
| Token verification (RS256, key cached) | median 0.035 ms, p95 0.052 ms |
| `identity_for` (identity plus roles) | median 4.0 ms, p95 5.3 ms |
| `ensure_session` on an existing session | median 2.5 ms, p95 4.5 ms |
| `session_subject` (the reset path's ownership read) | median 2.7 ms, p95 3.8 ms |
| JWKS fetches per 50 verifications | 1 |

Signature verification is not the cost; the database reads are. A `/chat` turn pays about
6.5 ms more than it did under M2 - the identity read plus the ownership read - against a
model call measured in seconds. The key set is fetched once per hour rather than per
request, and an unknown key id triggers at most one refetch per minute, so a stream of
forged key ids cannot be turned into a stream of requests to the identity provider.

### 3.5 Not verified

- **No regression in evaluation scores.** The evaluation loop drives `bench_agent.py`
  directly and never constructs the FastAPI application, so no token is verified and no
  identity is read during a benchmark run. The guard's new permission check does run
  there, and it is a no-op by construction: `SessionState` defaults to the customer
  permissions, which allow exactly what M0.3 allowed. The 31 `bench_agent` tests and the
  three baseline checksums are unchanged. The threshold is therefore marked not
  applicable, not passed: no run was made, and none is claimed.
- **A real Cognito user pool.** Issuer, audience, key id and signature are verified
  against a locally generated key pair. The pool, its rotation schedule and the network
  fetch are M6.
- **Token revocation before expiry.** Roles can be revoked and take effect immediately,
  but a token itself stays valid until `exp`. Cognito's own revocation endpoint and a
  deny list are not implemented, which is why the leeway is 30 seconds and not longer.
- **Rate limiting and lockout.** Nothing here limits how many bad tokens a caller may
  present. The verification is cheap and the key cache is bounded, so the cost of a
  forged token is one signature check, but that is a mitigation, not a limit.
- **Multi-tenant scope.** `identities` maps a subject to one retail user. Organizations,
  tenants and the "trusted backend context decides the tenant" rule from the architecture
  document are not implemented, because there is one simulated organization.
- **Audit of refusals.** `audit_events` records executed writes, not 401s and 403s. The
  refusals are visible in the HTTP response and in the guard's reason, but there is no
  durable record of an attempt that never became an operation.

### 3.6 Design choices that could have gone the other way

- **`sessions.subject` is nullable.** The demo and evaluation paths legitimately have no
  subject, and rows created before this migration have none. The ownership check reads
  NULL as "unowned", which under authentication means unreachable rather than open,
  because a caller's subject is never empty.
- **Deleting an identity uses `ON DELETE SET NULL`, not `CASCADE`.** Removing an account
  must not remove the record of what it did. The session survives with no owner, which
  no authenticated caller can reach.
- **`/sessions/reset` needed its own permission.** It is the one route that crosses
  session boundaries, so ownership cannot govern it. Under authentication it requires
  `session:reset_all`, which only `supervisor` holds; with authentication off, the demo
  identity holds a `demo` role that includes it, so `scripts/reset-demo.ps1` keeps
  working. That role is listed in the same table as every other, rather than being a
  special case inside the API, so there is one place to read what any request can hold.
- **The `agent` role gains nothing over `customer` in this stage.** Acting on another
  customer's order requires a ticket assignment, which is M4. Granting a permission the
  code cannot honour would have made the matrix decorative.

### 3.7 What went wrong along the way

Two things, both caught before they reached a commit.

`scripts/provision_identity.py` printed the identity *before* applying a revocation,
because `--show` returned early. An operator running `--revoke supervisor --show` would
have been shown the role they had just removed, and concluded the revocation had failed -
or worse, that it had succeeded when checking a different subject. The fix is ordering:
changes first, then the report. It has a test named after the defect.

The FastAPI dependency was first written as a closure inside `create_app`, with the
`Annotated[...]` aliases local to it. Under `from __future__ import annotations` FastAPI
resolves an endpoint's annotations against module globals, so the alias was invisible, the
identity parameter silently became a query parameter, and every request answered 422. It
failed loudly in an existing test, and the dependency now lives at module level with what
it needs read from `app.state`.

## 4. Conclusion

**PASS.** Seven of the eight thresholds are met and measured; the eighth, no regression
in evaluation scores, does not apply because the evaluation loop does not construct the
service, which is stated rather than reported as a pass.

What this step adds, in one line: the service now knows who is calling, refuses a token
it cannot verify, derives what the caller may do from the database rather than from the
token, and keeps one user out of another's session and orders.

What it leaves open is M3's own boundary rather than a defect: the verifier has never
met a real user pool, and it will not until M6 provisions one.
