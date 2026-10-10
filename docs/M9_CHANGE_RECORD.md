# M9: MCP server change record

Status: **implemented and tested locally. Not deployed.** The plan is
[M9_MCP_SERVER.md](M9_MCP_SERVER.md); where this record and the plan differ, the code is
what is described here, and section 5 lists every difference.

## 1. Change card

Recorded on 2026-10-10 against `main` at `4591191`, with the plan commit `251b738` on top.

- Stage and step: M9, steps 1 to 6 of the plan, on branch `feature/mcp-server`.
- Type: feature. A second entry point; nothing existing is replaced.
- Problem: the order tools are reachable only through the LangGraph agent behind the
  FastAPI service. An MCP client cannot use them, and no other entry point enforces the
  confirmation and access rules.
- Single theme: expose seven upstream retail tools over MCP Streamable HTTP, decided by
  the existing `WriteGuard`, `Store` and `AccessScope`.
- Controls not touched: model, prompt, reasoning settings, the τ²-bench evaluation and
  its baselines, the agent's behaviour, `WriteGuard`. `Store` changes in one method the
  plan names (`open_confirmation`) and gains one read (`confirmation_for`).
- Expected benefit: a client model can look up, cancel and return orders with the same
  guarantees as the application. Cost: one more process, one more dependency
  (`mcp` 2.3.0), a slower test suite.
- Primary metric: the plan's acceptance table, ten scenarios, all passing in both the
  2026-07-28 protocol and the legacy handshake.
- Protective metrics: no new failure in the existing suite; the access-control set
  `access-v2` still 22/22 on the agent path; the three frozen baselines verify.
- Reference: the existing suite on `4591191`. No model score is involved, so the MVP and
  `baseline-v3` comparison does not apply (section 3).

## 2. Implementation and verification

### Checks before starting

The plan asks for three facts before implementation.

1. **No version conflict with `mcp` 2.x — confirmed.** `mcp` 2.3.0 declares
   `Requires-Python >=3.10`, `pydantic>=2.12.0`, `starlette>=0.27`, `uvicorn>=0.31.1`
   and `pyjwt[crypto]>=2.10.1` (`.venv/Lib/site-packages/mcp-2.3.0.dist-info/METADATA`,
   lines 23-39). The lock already held pydantic 2.13.5, starlette 1.6.0, uvicorn 0.52.4
   and pyjwt 2.13.0 on Python 3.12. `uv add "mcp>=2"` added four packages (mcp,
   mcp-types, sse-starlette, and pywin32 on Windows only) and changed no existing
   version. FastAPI is not a dependency of `mcp`.
2. **`get_access_token()` works inside a `Resolve` resolver — confirmed; the permission
   pre-check stays in the resolver.** The token is a contextvar set by
   `AuthContextMiddleware` for the HTTP request (`mcp/server/auth/middleware/
   auth_context.py`, lines 10-20 and 35-43). The dispatcher runs each handler inside the
   sender's context snapshot (`mcp/shared/jsonrpc_dispatcher.py`, lines 692-707;
   `mcp/server/runner.py`, lines 655-658), and the SDK itself calls
   `get_access_token()` during request handling to bind `request_state` to the principal
   (`mcp/server/request_state.py`, lines 80-91). A throwaway script then served a
   resolver over a real port: the subject arrived in the resolver and the tool body in
   both protocols. The same run showed the 2026-07-28 path runs a resolver once per round
   (`mcp/server/mcpserver/resolve.py`, lines 10-16), which is why `open_confirmation`
   must be idempotent.
3. **Whether the demonstration client supports elicitation forms — confirmed manually on
   2026-10-10 with Claude Code 2.1.296.** Every write is also tested automatically against
   a client that declares form elicitation and one that does not. The manual check ran the
   server's core and tools against a disposable PostgreSQL, with the token verifier
   swapped for a local key pair instead of a Cognito pool, and the server added with
   `claude mcp add --transport http ... --header "Authorization: Bearer <token>"`:

   | Call | Form shown | Answer | Result | Database |
   |---|---|---|---|---|
   | `cancel_pending_order` `#W4135875` | yes | accept | `executed`, `replayed: false`, `operation_id: 1` | 1 operation, 1 confirmation `confirmed`, 1 `write_executed` audit row |
   | `return_delivered_order_items` `#W4817420` | yes | decline | `declined` | no operation, no audit row; the confirmation stays `pending` |
   | the same return again | yes | decline | `{"status":"declined","reason":"The user did not confirm this action. Nothing ran."}` | the pending confirmation reused, no new row |

   The form names the server ("MCP server “retail-support” requests your input"), lists
   the tool and every argument as the server described them, and offers Accept and
   Decline; the `ok` box is required. Claude Code's own tool-permission prompt comes
   first and is separate. Which protocol version Claude Code negotiated was not recorded.

Two SDK facts the plan did not cover were checked in the same reading:
`request_state` is sealed by default with a process-local AES-GCM key and bound to the
token's principal (`mcp/server/mcpserver/server.py`, lines 237-246;
`request_state.py`, lines 94-140), so a client cannot forge a recorded "accept"; and a
client without form elicitation makes the SDK raise `MCPError` from the resolver
(`resolve.py`, lines 673-708), which this server pre-empts with a reason (section 5).

### Commits

| Commit | Content |
|---|---|
| `cee1faa` | Formats the plan's Python skeleton; `ruff format --check .` failed on it alone |
| `3c161fe` | Step 1: dependency, `mcp_server.py`, four read tools over Streamable HTTP |
| `a380a02` | Step 2: `CognitoTokenVerifier` over `AuthConfig.verify`; `McpSettings`; `main()` |
| `d8985f0` | Step 3: per-call session from the database; bounded domain cache with replay |
| `000c99a` | `Store.open_confirmation` idempotent for one proposal; `Store.confirmation_for` |
| `2ef4136` | The handoff becomes `retail_support_agent.handoff`, used by the agent |
| `156e5a1` | Step 4: the two writes with form confirmation, and the handoff tool |
| `2b0408f` | Step 5: acceptance module, script, tests, CI step |
| `c0e0630` | The acceptance report of 2026-10-10 |
| this commit | Step 6: README section and this record |

### Files

- `src/retail_support_agent/mcp_server.py`: settings, token verifier, `RetailMcp` (the
  transport-independent core), the seven tools and their resolvers, `main()`.
- `src/retail_support_agent/handoff.py`: ticket and replies shared with the agent.
- `src/retail_support_agent/store.py`: `open_confirmation`, `confirmation_for`.
- `src/retail_support_agent/agent.py`: calls `handoff`; behaviour unchanged.
- `src/retail_support_agent/mcp_acceptance.py`, `scripts/run_mcp_acceptance.py`: the
  acceptance table as code, and its report.
- `tests/test_mcp_server.py`, `test_mcp_sessions.py`, `test_mcp_writes.py`,
  `test_mcp_acceptance.py`; three tests added to `tests/test_persistence.py`. No
  existing test was changed or removed.
- `.github/workflows/ci.yml`: runs the acceptance script after pytest and keeps the
  report as an artifact. `.gitignore`: ignores that CI report.

### How a write runs

1. The resolver (before the tool body; on 2026-07-28 once per round) builds the call's
   state from the database and previews the guard's verdict on it. The preview is not
   counted and not audited. Unless the only thing missing is the user's confirmation,
   it asks nothing. Otherwise it records the pending confirmation and returns
   `Elicit(call.describe(), Confirm)`.
2. The tool body builds the state again. If the user accepted and ticked the form, it
   calls `Store.confirm`; only a confirmation the database accepts is used. It then makes
   the one counting review, which writes any refusal to `audit_events`.
3. An allowed write runs through `Store.execute_once`, keyed on
   `Store.confirmation_for` this argument set, so duplicates share a key.

### Environment

Windows 11, Python 3.12.12, `uv sync --locked --group bench`, `mcp` 2.3.0, verified
τ²-bench data. A disposable `postgres:17-alpine` container on `127.0.0.1:55439`, used
only through `RSA_TEST_DATABASE_URL`; test fixtures create and drop tables. No model
endpoint, no AWS, no key.

### Checks before and after

Before any change, on `main` plus the plan commit:

| Check | Result |
|---|---|
| `ruff check .` | passed |
| `ruff format --check .` | **failed**: one file, `docs/M9_MCP_SERVER.md` (the plan's skeleton). `main` itself passed. Fixed in `cee1faa` |
| `mypy` | passed, 95 source files |
| `pytest -m "not live_model"` | 1051 passed, 0 skipped, 50 s |

The full suite after each step:

| After | Passed | Failed | Skipped |
|---|---|---|---|
| Step 1 | 1055 | 0 | 0 |
| Step 2 | 1071 | 0 | 0 |
| Step 3 | 1083 | 0 | 0 |
| `Store` change | 1086 | 0 | 0 |
| Handoff refactor | 1086 | 0 | 0 |
| Step 4 | 1103 | 0 | 0 |
| Step 5 | 1125 | 0 | 0 |

Final: ruff check and format pass, mypy passes on 103 source files, 1125 passed in
115 s, `scripts/freeze_baseline.py --verify` verifies all three baselines,
`tests/test_access_eval.py` still reports `access-v2` 22/22 on the agent path. The
suite took 50 s before and 115 s after; about 55 s of the difference is the acceptance
table, which starts real HTTP servers and loads the retail data per session.

The single warning in both runs is Starlette's deprecated `anyio.abc.BlockingPortal`
alias, pre-existing. The acceptance script on Windows also logs
`ConnectionResetError: [WinError 10054]` from asyncio's Proactor transport when a
connection closes (`_ProactorBasePipeTransport._call_connection_lost`). It does not
affect any result and does not occur on Linux; it is reported, not suppressed.

The server entry point was started once with a placeholder Cognito issuer: `/mcp`
answered 401 to no token and to a malformed one, with a `WWW-Authenticate` header
naming `/.well-known/oauth-protected-resource/mcp`, which listed the configured issuer.

**With the real user pool, after the merge (2026-10-10).** The entry point
`python -m retail_support_agent.mcp_server` ran unmodified against a disposable
PostgreSQL, with `RSA_AUTH_ISSUER` and `RSA_AUTH_AUDIENCE` from the base layer's
`cognito_issuer` and `cognito_client_id`, and the key set fetched from the pool. The
`ava` and `ivan` subjects were provisioned with `scripts/provision_identity.py`. The
token was `ava`'s access token from `initiate-auth` (`USER_PASSWORD_AUTH`): it carries
`token_use: access` and `client_id`, and no `aud`. It was read from a file and never
printed, and deleted afterwards.

| Check | Result |
|---|---|
| No token; a malformed token | 401, 401 |
| The real token with its signature altered | 401 |
| The real token with `sub` changed to `ivan`'s, signature kept | 401 |
| Tools listed | 7, in both protocols |
| `get_order_details` on `ava`'s own `#W4135875` | `ok`, in both protocols |
| `get_order_details` on `ivan`'s `#W8770097` | `denied_slice`, in both protocols |
| `cancel_pending_order`, the form declined | `declined`, one form per protocol |
| Database afterwards | 0 operations; 2 `access_denied` audit rows; 1 pending confirmation, reused by the second protocol's call |

An expired Cognito token was not waited for; expiry is covered by the verifier's tests
with a signed token.

### The acceptance table

From [`reports/mcp_acceptance/2026-10-10.json`](../reports/mcp_acceptance/2026-10-10.json),
produced by `scripts/run_mcp_acceptance.py` from `2b0408f` with a clean working tree in
100 s. Identical in both protocols ("legacy" negotiates 2025-11-25):

| Scenario | Expected | Measured, each protocol | Pass |
|---|---|---|---|
| No token / expired / wrong signature | 401, no tool runs | 401 / 401 / 401; tool executions 0; sessions opened 0 | yes |
| Read another customer's order | `denied_slice` | `denied_slice`; refusals 1; new audit rows 1 (`access_denied`) | yes |
| Cancel another customer's order | `denied_ownership` | `denied_ownership`; elicitations 0; tool executions 0 | yes |
| Cancel with a role lacking write | `denied_permission` | `denied_permission` (role `auditor`); elicitations 0 | yes |
| User declines, user cancels | `declined` | `declined` both; elicitations 1 each; `db_hash` unchanged; operations 0 | yes |
| User accepts | `executed` | `executed`; `db_hash` changed; `write_executed` rows 1; operations 1 | yes |
| Confirm, then change an argument | asked again | elicitations 2; the two questions differ; operations 1 | yes |
| One confirmed call, 8 concurrent | one execution | executed 1, replayed 7; domain cancellations 1; elicitations 0 | yes |
| Client without elicitation | fails with a reason | `elicitation_unsupported` with a reason; tool executions 0; pending confirmations 0 | yes |
| The access-control set through MCP | all held | 17/17 blocked of 22; 17/17 consistent with the agent path; 5 not applicable; elicitations 0 | yes |

The decline row was run with both refusal actions the protocol has (`decline` and
`cancel`). In the attack rows the stand-in user accepts every form it is shown.

The access set is `access-v2`. Five cases cannot be observed at the MCP boundary and are
reported per case, not scored: four whose only expectations concern the agent's own
answer (`repeat-an-identifier-without-rereading-it`, `invent-an-order-identifier`,
`invent-a-user-identifier`, `invent-a-payment-method`), and
`control-echo-an-identifier-the-user-supplied`, which makes no tool call. Within the 17,
the `output_withheld` and `reply_excludes` expectations of
`carry-another-users-identifier-across-turns` are not applicable for the same reason, and
the three cases aimed at tools MCP does not register are checked as refused by the
protocol instead of by an `access_denied` row. Two tests show the replay fails when a
control is removed.

## 3. Comparison

| Metric | MVP | Previous passing version | Candidate | Conclusion |
|---|---|---|---|---|
| Task success rate | not applicable | `baseline-v3` 102/114 | not re-measured | the agent, model and prompt are unchanged |
| Safety violations | not applicable | `access-v2` 22/22 on the agent path | 22/22 on the agent path; MCP 17/17 of the applicable | no regression; new entry point holds |
| System-side P95 | not applicable | not re-measured | not measured for MCP | no latency claim is made |
| Cost per task | not applicable | unchanged | 0 billed calls | no model is called by the MCP server |
| Capability added | — | none | MCP entry point, 10/10 scenarios in both protocols | accepted |

No billed evaluation was run and none was needed: nothing that produces a benchmark
score changed. The raw results are the report above and the pytest runs listed in
section 2.

## 4. Failure analysis and conclusion

Defects found while implementing, each fixed before its commit:

- **A replaced confirmation would have run (plan skeleton).** The skeleton marks the
  call confirmed in memory whether or not `Store.confirm` succeeds. If another proposal
  replaced the pending one between the question and the answer, the answer to the first
  would run the first. Only a confirmation the database accepts is used; the test
  `test_a_replaced_proposal_confirms_nothing` holds it.
- **Double audit rows (plan skeleton).** The skeleton reviews in the resolver and again
  in the body, on the same state; a refused write would count two denials and write two
  audit rows, three on 2026-07-28. The resolver now previews on a discarded state.
- **A retry could have run twice.** Keying a write on the session's latest confirmation
  (`last_confirmation_id`, as the agent does) gives an earlier action a new key once a
  later one is confirmed. `Store.confirmation_for` keys on the action's own
  confirmation.
- **Closures cannot be resolvers.** The SDK evaluates annotations by name, so
  `Resolve(<closure>)` failed with `InvalidSignature`. Tools are module-level and reach
  `RetailMcp` through the server lifespan, as the plan's step 3 describes.

Limits that remain:

- The client is trusted to show the form to a person; the server cannot distinguish a
  client that answers automatically.
- The refused-action limit (5) is counted in memory per process, as in the API. The
  step, tool-call and time limits of `SessionLimits` are not applied to MCP calls: the
  client's model drives the loop and the server sees single calls.
- One process. The domain cache is per process; a second process would rebuild from
  `operations` on first use but not see the first process's later writes. Idempotency
  holds across processes, consistency of reads would not.
- A 2026-07-28 elicitation in flight does not survive a server restart: the sealing key
  is process-local, and the client has to call again.
- A declined confirmation stays `pending` until the next proposal replaces it, as on the
  agent path; it keeps the idle check from reporting idle.
- Tokens are configured as a static header in the client; there is no OAuth flow.
- Measured on Windows locally. CI on Linux ran for #72: 1123 passed and 2 skipped, the
  same two that skip on `main` (1049 passed, 2 skipped before this change), and the
  acceptance script reported all passed.

**PASS for local implementation and acceptance.** All ten scenarios pass in both
protocols, no existing test fails, and the baselines verify. Claude Code was confirmed
manually to show the form and to run a write only on Accept, and the server accepts the
real user pool's access tokens and refuses altered ones. Not deployed.

## 5. Differences from the plan

| Plan | Code |
|---|---|
| Skeleton imports `ElicitationResult`, `AcceptedElicitation` from a path "to be checked" | Both are exported by `mcp.server.mcpserver` |
| Resolver reviews on the session state | Resolver previews on a discarded state; only the body's review counts and audits |
| Body confirms in memory, then reviews | Body uses only a confirmation `Store.confirm` accepted; a replaced one returns `needs_confirmation` |
| Execute with `session.confirmation_id` | Execute with `Store.confirmation_for(session, fingerprint)`, a new read on `Store` |
| Tools as decorated functions with closures | Module-level functions; `RetailMcp` held in the lifespan |
| Refusals use `Decision` values | They do; other outcomes add `ok`, `tool_error`, `executed`, `declined`, `needs_confirmation`, `elicitation_unsupported`, `handed_off`, `waiting_for_human`, `stopped` |
| Client without elicitation: "the write tool fails with a reason" | A normal result `elicitation_unsupported` with a reason, checked before the SDK would raise `MCPError`; no pending confirmation is left |
| "The existing 22 cross-customer cases" | `access-v2` has 22 cases, not all cross-customer (controls, fabrication, injection). 17 are observable over MCP; 5 are reported as not applicable |
| Compare with "the FastAPI entry" | Compared with `access_eval.run_case`, the `RetailAgent` path the FastAPI routes drive; not over HTTP |
| "The same confirmed call, 8 concurrent" | An accepted form executes in the same call, so the confirmed-not-yet-run state is recorded through `Store`, then 8 calls race |
| `mode="legacy"` | Negotiates protocol 2025-11-25 |
| Probe limit not mentioned | Implemented, in memory, as in the API |
| `AuthSettings` | `validate_token_resource=False`: the verifier checks `client_id`/`aud` itself |
| One small PR per step | Commits on one branch; nothing pushed, no PR, as instructed |
| Estimated four working days | Not tracked |
