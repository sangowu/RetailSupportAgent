# Architecture, data flow and permissions

## Logical boundaries

```text
Gradio / API client
  -> FastAPI: identity, session, request validation
  -> LangGraph: understand the request, ask for missing details, choose tools, confirm,
     stop or hand off
  -> business tools: permissions, resource ownership, policy, state, argument validation
  -> business data layer: query or transactional modification
  -> tool result -> the agent reports the actual outcome
```

M0 connects to an isolated Retail simulation. Application persistence and the standard
benchmark keep separate execution adapters. They share the protocol and reusable logic,
but migrating a database is never a reason to rewrite the official evaluation
expectations.

## Application state and storage (from M1)

Create the minimum tables the business actually needs rather than building a general
platform in advance:

- identities / role_memberships: Cognito subject to internal user, roles, organization scope.
- tickets / assignments: tickets and who they are assigned to.
- conversations / tasks: session and task state, claiming, failure information.
- confirmations / approvals: the action, an argument digest, who requested and who
  confirmed, validity, state.
- operations / audit_events: idempotency key, business outcome, authorization basis,
  trace id.
- Simulated order tables: M1 states the migration mapping explicitly and keeps the
  original fixture and a reset entry point.
- LangGraph checkpoint tables: use the verified official persistence implementation,
  separate from business authorization records.

Foreign keys, unique constraints, transactions and conditional updates carry the
business invariants. Organization scope is decided by trusted backend context; a client
cannot select an arbitrary tenant. The first version may have a single simulated
organization, with no tenant registration or billing system.

## Permission rules v0.1 (drafted by the developer)

| Role | Read | Modify | Manage |
|---|---|---|---|
| Customer | Their own orders and tickets | Confirmed, policy-compliant cancel or return on their own orders | None |
| Agent | Tickets assigned to them and the related orders | Operations the customer confirmed that comply with policy | May request escalation; cannot grant themselves permissions |
| Supervisor | Team tickets, related orders and audit within the same organization | Approve escalation requests within policy | Assign and reassign tickets; cannot confirm on a customer's behalf |

Application roles are granted by an administrator through controlled initialization; a
client cannot choose the supervisor role at registration. Account lifecycle, IAM
operations permissions and support business roles are separate: a business supervisor
cannot manage AWS keys or evaluation baselines.

The tool set is the intersection of what the account is authorized to do and what the
current task requires. The LLM recognizing a task cannot widen that authorization.
Hiding a tool is only an aid; the real check happens on every backend call.

## Confirmation and escalation rules

- A write such as a cancellation or return first states the order, items and arguments
  and obtains the customer's confirmation. Changing an argument expires that confirmation.
- Preconditions such as order status and quantity follow the pinned Retail policy
  exactly, with no added exceptions by amount.
- A handoff record is created when the customer asks for a human, when the agent cannot
  resolve the request within its allowed tools and steps, or when business information
  conflicts.
- Reassigning a ticket to another agent is a supervisor action; an agent cannot widen
  their own data visibility.
- Escalation approval is for a human to review and decide what happens next. It does not
  grant the ability to breach policy, and after a refusal the original action does not
  proceed.
- Even after approval, the action re-checks the current order state, the customer's
  confirmation, resource ownership and the current role. A pending confirmation or
  approval is valid for 30 minutes by default, configurable and covered by tests; once
  expired it must be confirmed again.
- No automatic approval rule for large refunds, because the MVP does not move real money.

## Concurrency, idempotency and recovery

- A confirmation produces a concrete operation id. A retry of the same operation reuses
  it; changed arguments require a new confirmation and a new operation.
- One session is processed serially. Different sessions may run concurrently within
  limits and never share mutable in-memory context.
- The business write and the operation record commit in the same transaction. If the
  response is lost, the idempotency record returns the original result.
- If an external write API is ever called, exactly-once depends on that API's idempotency
  mechanism or on reconciliation. A local transaction does not provide it.
- A checkpoint does not replace a business transaction. Re-entering a node during
  recovery must consult the idempotency state.
- M2 defines worker claiming with a persistent task table, using leases or an equivalent,
  so a restart or a second instance does not claim the same work twice. A timeout may
  reclaim work, but the operation's outcome must be re-verified.
- Automatic retries apply only to errors known to be retryable. Argument, permission and
  policy errors are not retried, and every loop has a bound.

## Logging and sensitive information

Langfuse records agent and tool performance, PostgreSQL holds the durable business
audit, and CloudWatch holds runtime errors. They are correlated by request_id, task_id,
operation_id and trace_id.

Logs must never contain an API key, JWT, password or session cookie. Only simulated
customer data is used, and identity fields are recorded to the minimum necessary.
Reasoning content follows the provider's conversation protocol, and the display and
archival policy distinguishes what the protocol requires from unnecessary logging.

## Failure scenarios that must be verified

Cross-user reads; forged roles; expired, wrong-issuer or wrong-audience credentials;
duplicate submissions; concurrent cancellation; state changing after confirmation; an
approver losing their permission; an operation succeeding while the response is lost; a
database failure mid-transaction; a model timeout; a process restart during approval;
a data reset affecting another account. Tests are implemented per stage, and any
capability not yet implemented is marked as not covered.
