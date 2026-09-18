# Architecture and access control

This guide describes the implemented application. Historical design proposals and
stage-specific results are retained in the linked reports. The system operates on
simulated retail data and does not integrate with a production order or payment service.

## Components and request flow

```text
Gradio UI / API client
  -> FastAPI: authentication, session ownership and request validation
  -> LangGraph agent: model calls, tool proposals and conversation state
  -> WriteGuard: permissions, access scope, order ownership, approval and confirmation
  -> RetailDomain: pinned tau2-bench tools, policy and simulated data
  -> tool result: response to the agent
  -> output check: identifiers the turn did not read are withheld from the reply

PostgreSQL: application state, operation records, approvals and audit events
```

The application uses one agent. FastAPI coordinates sessions and enforces access at the
API boundary. The agent proposes tool calls; backend checks determine whether a call may
execute. The model cannot grant permissions or substitute its own assertion for explicit
confirmation. The UI presents streaming responses, interaction states and case-management
controls.

`RetailDomain` wraps the pinned upstream tool implementations and policy. Each session
uses an isolated simulated retail environment. The application and benchmark have
separate execution adapters, allowing project-specific controls to be tested without
modifying upstream evaluation expectations.

## Persistence and recovery

When `RSA_DATABASE_URL` is configured, SQLAlchemy and Alembic manage these application
records in PostgreSQL:

| Tables | Responsibility |
|---|---|
| `identities`, `role_memberships` | Map authenticated subjects to retail identities and roles |
| `sessions`, `session_claims` | Session ownership, execution counters and worker leases |
| `messages` | Persist visible conversation turns |
| `confirmations` | Track confirmation of a specific tool and argument fingerprint |
| `operations`, `audit_events` | Record idempotency keys, operation results, executed writes and refused access |
| `tickets`, `ticket_assignments` | Track cases, current assignments and assignment history |
| `approvals` | Record escalation requests, decisions and expiry |

Retail orders remain in the per-session upstream simulation. Recovery reconstructs that
environment by replaying successful recorded operations against the pinned initial data.
It also restores visible messages, confirmations and execution limits. The implementation
uses this application-level recovery mechanism; it does not persist the graph through a
separate LangGraph checkpoint store.

Without a database URL, the service operates with in-memory sessions. Database constraints,
transactions and conditional updates enforce the invariants of the persistent application
records. Organization-level tenancy is not implemented.

## Authentication and permissions

With `RSA_AUTH_ISSUER` and `RSA_AUTH_AUDIENCE` configured, the API verifies the token and
maps its subject to an application identity. Roles are read from the database rather than
from token group claims. Application routes require authentication; `/health` and `/idle`
remain operational exceptions. When authentication is disabled, a fixed local demo
identity is used.

The current permission definitions are in `src/retail_support_agent/auth.py`:

| Role | Order access | Case management | Additional permissions |
|---|---|---|---|
| `customer` | Read and confirmed writes for their own retail identity | No case-management permissions | None |
| `agent` | Own orders and orders associated with currently assigned cases; writes remain guarded | Read and work assigned cases | Can request approval while working a case |
| `supervisor` | Order permissions remain subject to resource checks | Read, assign and reassign cases; decide escalations; cannot work cases | Audit access and reset-all permission |
| `auditor` | None | None | Read-only audit access |
| `demo` | Fixed simulated identity for local operation | None | Reset-all permission; not a provisioned Cognito role |

API permissions and resource ownership are independent checks. A valid token does not
grant access to another account's conversation. An agent's access to a customer's orders
requires a current ticket assignment. Audit access is permission-based across application
sessions; it is not protected by an organization boundary in this single-organization demo.

### Access scope

A permission states that an account may read order data. The access scope states whose.
`src/retail_support_agent/scope.py` defines the slice one session may reach, and
`WriteGuard` applies it to reads as well as writes:

| Axis | Derived from | Default |
|---|---|---|
| Row: identity | The authenticated identity, or the customer named by an assigned ticket | The caller's own data |
| Row: order stage | `status` on the order record | Every stage (`RSA_SCOPE_WRITE_STATUSES`) |
| Column | The field name in the tool result | `email` hidden (`RSA_SCOPE_REDACT_FIELDS`) |
| Output | Identifiers the turn actually read | Checked (`RSA_SCOPE_CHECK_OUTPUT`) |

The simulated dataset has no tenant, sensitivity or date column, and it is SHA256-verified,
so a marker cannot be written into it. Row slices are therefore derived from fields the
records already carry (`scope.slice_key`), and column slices use the field name as the
label. Catalog reads (`get_product_details`, `get_item_details`, `list_all_product_types`,
`calculate`) are outside the slice: that data is identical for every account. Identity
lookups by email or by name and zip are refused under an enforcing scope, because the
identity already came from a verified token.

There is a fourth check, on the way out. Everything above decides what the model may
*reach*; `AccessScope.withhold` decides what may leave. The model's answer is compared
against the identifiers this turn actually read - those in its tool results, plus those
the user typed, since echoing an id back to the person who supplied it discloses nothing -
and anything else is replaced before the reply is returned or journaled.

The comparison is per turn, not per session, and that is the point of it. The
conversation history still holds what earlier turns read, and a role can be revoked
between two turns; without this, a model could answer the second turn from what it
learned in the first, under permissions the account no longer holds. What it catches is
worth stating plainly: identifiers, so a fabricated or carried-over order number is
caught, while content paraphrased without its identifier is not. It narrows the channel;
it does not close it. It is the riskiest of these controls - it can withhold an identifier
from an answer that was in fact legitimate - so `RSA_SCOPE_CHECK_OUTPUT` turns it off
without giving up the row and column slices. Withheld identifiers are recorded in the
slice audit: an answer altered on the way out is exactly the event a reviewer needs.

Redaction applies to what the model is shown, not to what is recorded: operation results
are stored unredacted, because recovery replays them. A refused read returns the same
wording whether the order belongs to somebody else or does not exist, so refusals cannot
be used to enumerate order ids.

The scope is rebuilt on every turn from the identity and the current assignment, for the
same reason roles are re-read rather than taken from the token. The benchmark adapter
keeps a permissive scope: it discovers the customer's identity mid-conversation through
`find_user_id_*`, so an enforcing default would deny the lookups the evaluation depends
on and invalidate the recorded baselines. Slice verdicts are recorded per session in
`SessionState.slice_audit`, and the agent drains that list at the end of every turn into
`audit_events` as `access_denied` and `output_withheld`. Allowed reads are not written: a
row per read would bury the table, which is the same reason reads are not recorded as
operations. The guard itself never touches the database - it stays pure logic that a test
can drive without one - so the draining happens in the agent. Access records are written
after the turn, in their own transaction: an audit failure must not roll back a write the
customer already confirmed, and the cost of that choice is that a crash between the turn
and the write loses that turn's access records.

### Availability of the permission source

Roles and identities are read from the database on every request; nothing caches them.
The consequence is that there is no stale permission to fall back on when the database
is unreachable, and the request is refused rather than answered. Specifically, an outage
does not fall back to the demo identity, does not reuse the permissions an earlier turn
resolved, and does not report which component failed. With authentication configured and
no database URL, the service refuses to start rather than refusing one request at a time.

This is a property of not caching, not of an added circuit breaker, which is why
`tests/test_fail_secure.py` states it as a contract: an identity cache added later to
make a slow database feel faster would fail those tests.

A session also has a bounded number of refusals (`SessionLimits.max_denials`, default 5).
Each refusal is already correct, but the next one costs an attacker nothing, so a session
that keeps proposing calls it may not make is stopped. Confirmation and approval requests
are not counted - they mean "not yet", not "not allowed" - and neither is an invented tool
name, which is the model's mistake rather than an account reaching for data.

Roles are provisioned through administrative configuration and scripts, not a public
registration route. Business roles do not grant AWS infrastructure permissions. UI
visibility improves usability but does not replace backend enforcement.

## Confirmation, approval and handoff

A write proposal identifies a tool and its exact arguments. Confirmation is bound to that
fingerprint; changing the arguments requires confirmation of the new proposal. The current
confirmation model does not have a time-based expiry field.

When an agent acts through an assigned case, the write additionally requires a supervisor's
approval for the same argument fingerprint. Approval validity is controlled by
`RSA_APPROVAL_TTL_SECONDS`, defaulting to 1,800 seconds. Approval does not bypass tool
policy, order ownership or the separate confirmation requirement. The confirmation is
collected in the active session; an agent works in a separate session from the customer's
conversation.

A handoff creates a ticket and suspends the customer's session. A supervisor assigns the
case, and the assignee works it under their own authenticated subject. Self-assignment and
self-approval restrictions enforce separation between case work and approval decisions.
Resolving the ticket allows the customer to resume the original conversation.

The application reloads permissions, assignment context and approval validity when
processing subsequent turns. A pending or approved request cannot grant permissions that
the caller no longer holds. The upstream retail policy remains authoritative for order
state and operation preconditions.

## Concurrency and idempotency

A session is processed serially, with database-backed leases preventing competing workers
from processing it simultaneously. Expired leases can be reclaimed. Recovery preserves
execution counters and session age rather than resetting the configured limits.

An operation's idempotency key identifies the session, proposed action and confirmation.
A duplicate completed operation returns its recorded result. Operation and audit records
are committed transactionally. The simulated business state is reconstructed from those
records after restart; this is not a distributed transaction with an external order system.

Any future external write integration would require its own idempotency or reconciliation
contract. A local transaction cannot guarantee exactly-once execution in another service.
Concurrent-user capacity and deployment-level load limits have not been measured.

## Audit and observability

PostgreSQL stores the application audit trail: executed writes, refused access and
answers altered on the way out. `GET /session/{session_id}/audit` requires `audit:read`;
the deployed UI does not yet provide an audit-viewing page. The configured
demo accounts include a supervisor with audit permission but no dedicated auditor account.

Langfuse is used for evaluation tracing; the application service does not currently depend
on it. Runtime diagnostics are available through container logs and the instance's systemd
journal. This guide does not claim a deployed CloudWatch logging pipeline.

Credentials, tokens and passwords must not be included in logs or reports. Evaluation and
demo data are simulated. Preserve the configuration and raw evidence needed to reproduce
reported results without publishing credentials.

## Verification coverage and limits

Stage reports document database rollback and duplicate-submission checks in
[M1](M1_PERSISTENCE.md), session recovery in [M2](M2_RECOVERY.md), authentication and
cross-user access checks in [M3](M3_AUTH.md), and assignment, approval and expiry checks in
[M4](M4_HANDOFF.md). The controls are measured end to end by the frozen scenario set in
[M8 access evaluation](M8_ACCESS_EVAL.md) - twenty attempts, including three that must
still succeed. Permission-source outage behaviour is covered by `tests/test_fail_secure.py`, which needs
no database: it drives the API with a store that raises. Access-scope behaviour is covered
by `tests/test_scope.py`: slice
derivation against all 1,000 upstream order records, scope logic, guard refusals,
redaction as the model sees it, and a regression check that the benchmark adapter's scope
stays permissive. The effect of an enforcing scope on benchmark scores has not been
measured, because the benchmark does not run under one. UI behavior has handler-level tests and API-backed exercises; a
complete browser acceptance workflow remains outstanding.

Approval expiry has database and API coverage. A process restart during an approval flow
has not been established by a dedicated end-to-end test. Cross-schema rollback, concurrent
load and high-availability behavior are also outside the verified scope. See
[operations](OPERATIONS.md) for deployment-specific limitations and
[evaluation gates](EVALUATION_AND_GATES.md) for acceptance criteria.
