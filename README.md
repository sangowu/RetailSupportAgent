# RetailSupportAgent

A retail support application built with LangGraph, FastAPI and Gradio. It handles
simulated order enquiries, cancellations and return requests, with explicit confirmation
for state-changing actions, role-based access and human escalation workflows.

The project demonstrates how an LLM application can combine tool use with application-level
authorization, persistent state, recovery and reproducible evaluation. It uses simulated
retail data; it does not process real payments or connect to a production order system.

## Current capabilities

- **Customer support:** order lookup, cancellation and return requests, with streaming
  responses and explicit action confirmation.
- **Access control:** Cognito authentication, database-managed roles, session ownership
  and order-access checks enforced by the backend, with reads and writes held to a
  per-session data slice and sensitive fields hidden from the model. Refused access and
  altered answers are recorded and readable from the UI by accounts holding `audit:read`.
- **Human handoff:** case creation, supervisor assignment, agent work sessions, approval
  decisions and resumption of the customer's conversation after resolution.
- **Persistence and recovery:** PostgreSQL records for sessions, messages, confirmations,
  operations and audit events, with idempotent writes and session recovery.
- **Deployment:** containerized local services and a Terraform-managed AWS demonstration
  environment with HTTPS, automatic idle shutdown, database archives and image rollback.
- **MCP server (M9, local only):** the order tools for any MCP client over Streamable
  HTTP, behind the same token verification, roles, data slice, confirmation and
  idempotent writes as the API. See [MCP server](#mcp-server).

M0 through M9 delivery records are available below. The AWS deployment is configured for
image `m8.7`; it is operated on demand and is not continuously available. CI runs static
checks, tests with PostgreSQL, upstream benchmark verification and baseline integrity
checks. CI does not run paid model evaluations or deploy the application.

## Evaluation results

The accepted configuration uses `deepseek-v4-pro` with reasoning effort `high`. The table
reports recorded results on the pinned 114-task tau2-bench Retail set, using simulated
user `glm-5.3-flash` and judge `qwen3.7-flash` with JSON Schema.

| Agent | Successful tasks | Unconfirmed write events | Cost per task | Cost per successful task |
|---|---|---|---|---|
| Upstream `llm_agent` (`baseline-v2`) | 97/114 (85.09%) | 168 | 0.0855 CNY | 0.1004 CNY |
| Project agent with write controls (`baseline-v3`) | 102/114 (89.47%) | 0 | 0.0954 CNY | 0.1066 CNY |

Access controls are measured separately, by a frozen set of attack scenarios that needs
no model and no spend: `22/22 blocked (access-v2)`. A second, billed set runs
natural-language social engineering against the real agent with a real model on both
sides: `8/8 held (attacks-v3)`, 56 turns, nothing disclosed, indirect prompt injection included. The tau2 tasks contain no
access-control attempts, so they can show only that the controls did not break normal
business. See the [access evaluation](docs/M8_ACCESS_EVAL.md) for the cases, the controls
it includes alongside the refusals, and its limits.

These are benchmark observations, not production service guarantees. Costs reflect the
recorded runs and pricing periods; both runs used off-peak pricing. The full benchmark
includes tasks beyond the three primary workflows; the declared-scope subset contains
42 tasks. See the [measurement report](docs/M5.1_MEASUREMENT.md) for definitions and results.

The paired McNemar exact test returned `p = 0.3018`. The observed success-rate difference
was not statistically significant; this does not establish equivalence or non-inferiority.
No unconfirmed write events were observed for the guarded agent in that run.

A repeat run on 2026-09-09 scored 101/114. This provides an additional observation, not an
estimate of the configuration's overall variance. Lower-cost reasoning configurations
were evaluated and rejected under the project's acceptance criteria; see
[M5.2](docs/M5.2_THINKING_EFFORT.md) and [M5.3](docs/M5.3_THINKING_ROUTING.md).
The available evidence does not establish superiority over other models because a
controlled comparison on the same evaluation stack has not been completed.

## Local setup

Requirements: Python 3.12, uv and Git. PowerShell 7 is required for the local process
scripts; Docker is required for the container stack and a local PostgreSQL service.
Run commands from the repository root unless a guide specifies otherwise.

```bash
uv sync --locked --group bench
uv run python scripts/fetch_tau2.py
```

Copy `.env.example` to `.env` and configure the required values, including
`DASHSCOPE_API_KEY`. Credentials must remain outside version control.

For the local API and UI processes:

```powershell
pwsh scripts/start-local.ps1
pwsh scripts/stop-local.ps1
```

The default addresses are `http://127.0.0.1:8000` for the API and
`http://127.0.0.1:7860` for the UI. Without authentication settings, the application uses
a fixed simulated identity and must remain local. Startup and health checks do not call
the model; sending chat messages can incur provider charges.

To enable application persistence:

```bash
docker compose up -d db
# Set RSA_DATABASE_URL in .env to the intended local PostgreSQL database.
uv run alembic upgrade head
```

Without `RSA_DATABASE_URL`, sessions use in-memory storage. Alternatively,
`docker compose up -d --wait` starts the containerized database, migrations, API and UI.
See [operations](docs/OPERATIONS.md) for configuration, reset scope and shutdown behavior.

## MCP server

A second entry point next to the API, for MCP clients such as Claude Code. The client's
own model is the agent: no model is called here and the LangGraph agent is not involved.
What decides whether a call may run is the application's own code - `WriteGuard`,
`Store` and `AccessScope` - not anything the client says. Implemented and tested locally
in M9; it is not deployed to AWS.

| Tool | Kind | Checked before it runs |
|---|---|---|
| `get_user_details`, `get_order_details` | read | role, then the caller's own data slice; `email` hidden |
| `get_product_details`, `list_all_product_types` | read | catalog, open to every account |
| `cancel_pending_order`, `return_delivered_order_items` | write | role, ownership, status slice, the user's answer in a form, once per confirmation |
| `transfer_to_human_agents` | handoff | opens a ticket; the session then runs no tool until a person closes it |

Names and parameters are upstream τ²-bench's. The two account lookups are not offered,
and exchanges and address, payment or item changes are left for a later version.

### Start

The server needs PostgreSQL and a Cognito user pool, and refuses to start without
either. It reads the API's variables, and no model key:

```bash
# RSA_DATABASE_URL, RSA_AUTH_ISSUER and RSA_AUTH_AUDIENCE as for the API.
# Optional: RSA_MCP_HOST (127.0.0.1), RSA_MCP_PORT (8765), RSA_MCP_URL, RSA_AUTH_JWKS_URL.
uv run python -m retail_support_agent.mcp_server
```

The endpoint is `http://127.0.0.1:8765/mcp`. A caller is the `sub` of a Cognito access
token, and must be provisioned like an API user:

```bash
uv run python scripts/provision_identity.py --subject <sub> --user-id <retail id> --role customer
```

### Connect from Claude Code

```bash
claude mcp add --transport http retail-support http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <Cognito access token>"
```

The token is passed as a header and expires with the user pool's access-token lifetime;
add the server again with a new token when it does. Claude Code (checked with 2.1.296)
shows the confirmation form after its own tool-permission prompt: the tool, every
argument, a required `ok` box, Accept and Decline. A client without form elicitation gets
`elicitation_unsupported` from every write and nothing runs.

### Trust boundary

- **Who** comes only from the verified token. A missing, expired or wrongly signed token
  is answered 401 before any tool. Roles come from the database, never the token, so a
  revoked role stops working on the next call.
- **What** is decided by the server on every call. Refusals are returned as
  `{"status": "<decision>", "reason": "..."}`, written to `audit_events`, and five of them
  stop the session, as in the API.
- **Confirmation** is a form the client shows to the user, listing every argument. The
  model cannot answer it, and an answer passed as a tool argument is ignored. A
  confirmation covers exactly one argument set; changing any argument asks again.
- **The client is trusted to show the form to a person.** The server cannot tell a client
  that shows the form from one that answers it automatically. Connect only clients you
  would let act on the account with the user's consent.
- **Repeats run once.** A retried or duplicated confirmed call returns the stored result
  (`"replayed": true`), whichever protocol or process it arrives through.

The acceptance table - ten scenarios, each in the 2026-07-28 and the legacy protocol -
is measured by `uv run python scripts/run_mcp_acceptance.py` into
`reports/mcp_acceptance/`, and asserted by `tests/test_mcp_acceptance.py`. See the
[M9 change record](docs/M9_CHANGE_RECORD.md).

## Verification

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not live_model"
uv run python scripts/freeze_baseline.py --verify
```

Database tests require `RSA_TEST_DATABASE_URL` pointing to a dedicated PostgreSQL test
database. Without it, database-dependent tests are skipped. Do not use an application or
shared database for tests. CI supplies an isolated PostgreSQL service. Tests that require
untracked historical evaluation traces may also skip in a fresh checkout.

The following commands inspect existing results or preview requests without model calls:

```bash
uv run python scripts/probe_models.py --dry-run
uv run python scripts/analyze_eval.py --project 74
```

The following commands make model calls and incur provider charges:

```bash
uv run python scripts/demo_flows.py
uv run python scripts/run_eval.py --pilot
```

Evaluation comparisons must identify the agent, simulated user, evaluator, task set and
model parameters. Changing the evaluation stack can invalidate a direct score comparison.

## Deployment and operational limits

The AWS demo uses a single EC2 instance with PostgreSQL on its root volume, ECR images,
Cognito authentication, Caddy HTTPS and S3 database archives. It is a demonstration
topology without database replication or high availability.

```bash
uv run python scripts/demo.py status
uv run python scripts/demo.py start
uv run python scripts/demo.py stop
```

AWS operation requires an existing environment and appropriate credentials. Starting an
instance incurs infrastructure charges; retained resources can incur charges while it is
stopped. Review the [Terraform setup guide](deploy/terraform/README.md) for provisioning
and the [operations guide](docs/OPERATIONS.md) for release and archive procedures.

Browser acceptance now covers sign-in, the sections each account is offered, refusing a
cross-customer read and reading the resulting audit record, plus handing a case off,
assigning it, working it, escalating a write, deciding it and closing the case. It is
recorded in the [M8 release](docs/M8_RELEASE.md), and it found eight defects that no test
in this repository could have found - two of them in earlier fixes for the same defects.

A ninth was found by constructing concurrent requests rather than by using the interface:
one session could run two turns at once inside a process, both callers were answered, one
of their messages was dropped from the record, and the stop-condition counters advanced
once for two turns. One session now runs one turn at a time, and a second message is
refused with the same 409 a second process gets.

Current validation limits include approval expiry and the intermediate streaming states,
which are covered at the database and API levels but not by a browser workflow; load, in
the sense of throughput and many sessions at once; and rollback across a schema change on
the deployed instance, which was validated locally against a database the previous image
had built but has not been exercised in the deployed environment. The user-data
reconciliation verified deployment metadata, not the installed script or application
behavior at runtime.

## Documentation

| Guide | Purpose |
|---|---|
| [Architecture and access control](docs/ARCHITECTURE_AND_ACCESS.md) | Components, persistence, authorization and recovery |
| [Operations](docs/OPERATIONS.md) | Local services, AWS commands, account management and archives |
| [Terraform setup](deploy/terraform/README.md) | Environment provisioning, image publishing and teardown |
| [Evaluation and gates](docs/EVALUATION_AND_GATES.md) | Metrics, baselines and acceptance criteria |
| [Project context](PROJECT_CONTEXT.md) | Recorded goals and scope decisions |
| [Development plan](docs/DEVELOPMENT_PLAN.md) | Staged delivery plan |

### Historical reports

Reports retain the findings and limitations recorded at each stage. Statements about
pending work in an older report describe that revision; use the current guides and later
repair records for the present status.

- **Baseline and experiments:** [M0 report](docs/M0_REPORT.md),
  [protocol](docs/M0.1_PROTOCOL_FREEZE.md), [model capabilities](docs/M0.2_MODEL_CAPABILITY.md),
  [business loop](docs/M0.3_BUSINESS_LOOP.md), [selection experiments](docs/M0.4_EXPERIMENT_PLAN.md),
  [observability](docs/M0.5_OBSERVABILITY.md), [local demo](docs/M0.5_LOCAL_DEMO.md),
  [baseline freeze](docs/M0.5_BASELINE.md), [agent evaluation](docs/M0.6_AGENT_UNDER_TEST.md),
  [model experiment plan](docs/MODEL_EXPERIMENTS.md).
- **Application foundations:** [persistence](docs/M1_PERSISTENCE.md),
  [recovery](docs/M2_RECOVERY.md), [authentication](docs/M3_AUTH.md),
  [handoff and approvals](docs/M4_HANDOFF.md).
- **Measurement and optimization:** [cost and latency](docs/M5.1_MEASUREMENT.md),
  [reasoning effort](docs/M5.2_THINKING_EFFORT.md), [reasoning routing](docs/M5.3_THINKING_ROUTING.md).
- **Deployment:** [topology](docs/M6.1_DEPLOYMENT_SHAPE.md),
  [containers](docs/M6.2_CONTAINER_AND_START.md), [idle shutdown](docs/M6.3_IDLE_SHUTDOWN.md),
  [Terraform](docs/M6.4_TERRAFORM.md), [operator commands](docs/M6.5_START_STOP.md),
  [HTTPS](docs/M6.6_HTTPS.md), [login](docs/M6.7_LOGIN.md),
  [rollback](docs/M6.8_ROLLBACK.md), [archives](docs/M6.9_ARCHIVE.md).
- **User interface:** [streaming](docs/M7.1_STREAMING.md),
  [interaction states](docs/M7.2_STATES.md), [case management](docs/M7.3_TICKETS.md).
- **Access control:** [access evaluation](docs/M8_ACCESS_EVAL.md),
  [M8 release and browser acceptance](docs/M8_RELEASE.md).
- **MCP server:** [plan](docs/M9_MCP_SERVER.md), [change record](docs/M9_CHANGE_RECORD.md).
- **Delivery and maintenance:** [M1–M8 delivery report](docs/DELIVERY_REPORT.md),
  [CI repair](docs/CI_TYPE_FIX.md), [user-data reconciliation](docs/USER_DATA_RECONCILIATION.md).

Contributors should read [AGENTS.md](AGENTS.md). The
[change report template](docs/CHANGE_REPORT_TEMPLATE.md) defines the per-step evidence format.

## Benchmark attribution

This project uses [Sierra tau2-bench Retail](https://github.com/sierra-research/tau2-bench),
pinned to `v1.0.1` / commit `fc0055dc4e0a316c3f83133267fbd6faaa770992`
(MIT, Copyright (c) 2025 Sierra Research). Per-file checksums are recorded in
`evaluation/tau2_manifest.json`. Upstream data, tools and evaluators retain their original
attribution. Standard benchmark results and project-specific authorization, approval and
recovery tests are reported separately.
