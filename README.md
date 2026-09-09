# RetailSupportAgent

A retail customer-support agent: order lookup, order cancellation and return requests,
with business correctness, reliability and cost demonstrated through reproducible
evaluation.

**Status: M0 frozen (M0.1-M0.6), M1 to M4 implemented, M5.1 measurement in place, M5.2 and M5.3 tested and rejected three ways to spend less on thinking, M6.1 chose the AWS deployment shape from measurements, M6.2 packaged the application and timed its start, and M6.3 made the instance stop itself when nobody is using it. Nothing is deployed.**

The model is `deepseek-v4-pro` with thinking at effort=high.

| Subject under test | Full set (114 tasks) | Unconfirmed writes | Cost per task | Per successful task |
|---|---|---|---|---|
| upstream `llm_agent` (`baseline-v2`) | 97/114 = 0.8509 | 168 | 0.0855 CNY | 0.1004 CNY |
| this project's guarded agent (`baseline-v3`) | 102/114 = 0.8947 | **0** | 0.0954 CNY | 0.1066 CNY |

Costs are what each run actually spent, priced call by call at the rate in force when the
call was made; both ran off peak. See [M5.1](docs/M5.1_MEASUREMENT.md).

The same configuration repeated on 2026-09-09 scored 101/114, so the score's own spread is
about one task. Disabling thinking costs a third less and answers in a quarter of the time
at p95, and it was rejected: it gets six database outcomes wrong that the incumbent gets
right every time. See [M5.2](docs/M5.2_THINKING_EFFORT.md). Spending thinking only on the
turns that need it is refused by the provider mid-turn, and its ceiling is 24% of cost and
nothing at all on p95. See [M5.3](docs/M5.3_THINKING_ROUTING.md).

Paired McNemar exact test p = 0.3018, not significant: enforced confirmation did not
lower the score. The measurable gain is that 168 unconfirmed writes became 0.

M0's scores measured upstream's agent, not this project's; see
[M0 report §1.1](docs/M0_REPORT.md). `baseline-v1` (holdout 40 tasks, 0.8750) is
archived.

**Not built yet**: AWS deployment, a hosted demo, and any screen for the people who work
the handoff queue. Authentication is implemented and verified against a locally generated
key pair; it has not yet met a real Cognito user pool, which is M6. See
[M0 report §9](docs/M0_REPORT.md).

Model selection was a cost decision, not a quality comparison: the other candidates
were never measured on the same evaluation stack, and on the same holdout the cost
differed by 4.8x. Cross-model numbers from an earlier round were discarded because the
judge was not held constant; see
[M0.4 §9.9.1](docs/M0.4_EXPERIMENT_PLAN.md).

## Reading order

1. **[M0 report](docs/M0_REPORT.md)** - start here; conclusions and limits in one page
2. [Project goals and settled decisions](PROJECT_CONTEXT.md)
3. [M0.1 protocol and environment freeze](docs/M0.1_PROTOCOL_FREEZE.md)
4. [M0.2 model capability verification](docs/M0.2_MODEL_CAPABILITY.md)
5. [M0.3 minimal business loop](docs/M0.3_BUSINESS_LOOP.md)
6. [M0.4 selection experiment and cost estimate](docs/M0.4_EXPERIMENT_PLAN.md)
7. [M0.5 part one: cost accounting and observability](docs/M0.5_OBSERVABILITY.md)
8. [M0.5 part two: local start/stop and demo flows](docs/M0.5_LOCAL_DEMO.md)
9. [M0.5 part three: frozen baselines](docs/M0.5_BASELINE.md)
10. [M0.6 putting this project's agent under test](docs/M0.6_AGENT_UNDER_TEST.md)
11. [M1 application state in PostgreSQL](docs/M1_PERSISTENCE.md)
12. [M2 cross-process recovery](docs/M2_RECOVERY.md)
13. [M3 authentication, roles and resource permissions](docs/M3_AUTH.md)
14. [M4 tickets, assignment, approval and handoff](docs/M4_HANDOFF.md)
15. [M5.1 cost priced by the run's clock, and latency](docs/M5.1_MEASUREMENT.md)
16. [M5.2 how much thinking the task set needs](docs/M5.2_THINKING_EFFORT.md)
17. [M5.3 routing thinking per turn, and why it stops](docs/M5.3_THINKING_ROUTING.md)
18. [M6.1 choosing the AWS deployment shape](docs/M6.1_DEPLOYMENT_SHAPE.md)
19. [M6.2 packaging the application, and whether it starts in time](docs/M6.2_CONTAINER_AND_START.md)
20. [M6.3 deciding nobody is using this, and turning it off](docs/M6.3_IDLE_SHUTDOWN.md)
21. [MVP and staged development plan](docs/DEVELOPMENT_PLAN.md)
22. [Model capability checks and selection experiments](docs/MODEL_EXPERIMENTS.md)
23. [Baselines, metrics and CI gates](docs/EVALUATION_AND_GATES.md)
24. [Architecture, data and permissions](docs/ARCHITECTURE_AND_ACCESS.md)
25. [Local and AWS start/stop design](docs/OPERATIONS.md)
26. [Per-step change report template](docs/CHANGE_REPORT_TEMPLATE.md)

Developers and coding agents should read [AGENTS.md](AGENTS.md) first.

## Scope currently in progress

M0 established the evaluation protocol, the minimal business loop, and the model and
thinking-parameter experiments, and froze the baselines. M1 added application-state
persistence, M2 made a session survive a restart, M3 established who is calling and what
they may do, and M4 turned a handoff into a case a human owns and decides. M5.1 made
cost and latency measurable before anything is optimized against them. Each step
changes one theme and reports its effect against the current baseline and the previous
passing version. The roadmap is not an instruction to build
everything at once.

A quotable score requires naming its evaluation stack: simulated user `glm-5.3-flash`
plus judge `qwen3.7-flash` with JSON Schema. Replacing any role in that stack makes the
number incomparable: on 2026-09-07 the same tasks went from 0.600 to 0.875 with the
stack as the only difference.

## Local development

```bash
uv sync --group bench                        # includes the upstream τ²-bench package
uv run python scripts/fetch_tau2.py          # fetch the pinned commit and verify SHA256
uv run ruff check . && uv run mypy && uv run pytest
uv run python scripts/freeze_baseline.py --verify   # verify the baselines are unaltered
```

Commands that need `DASHSCOPE_API_KEY` and spend real money:

```bash
uv run python scripts/probe_models.py --dry-run   # capability probes: see the request count first
uv run python scripts/demo_flows.py               # the three flows plus an ownership refusal
uv run python scripts/run_eval.py --pilot         # 2-task evaluation smoke run
uv run python scripts/analyze_eval.py --project 74  # usage and cost projection
```

Local services (bound to 127.0.0.1, fixed simulated identity, no public registration):

```powershell
pwsh scripts/start-local.ps1     # check prerequisites, start :8000 and :7860, print URLs
pwsh scripts/reset-demo.ps1      # reset demo sessions (deletes no files)
pwsh scripts/stop-local.ps1      # stop only the processes this project recorded
```

Starting costs nothing; only sending a message is billed. What the scripts do and why
is in [M0.5 part two](docs/M0.5_LOCAL_DEMO.md).

Application state (M1) needs PostgreSQL:

```bash
docker compose up -d db                      # local instance, loopback only
uv run alembic upgrade head                  # RSA_DATABASE_URL must be set
```

Without `RSA_DATABASE_URL` the service still runs with M0's in-memory sessions.

Authentication (M3) is off until an issuer and an audience are configured. With
`RSA_AUTH_ISSUER` and `RSA_AUTH_AUDIENCE` set, every route requires a bearer token, the
retail identity comes from the token's subject, and roles come from the database - never
from the token's group claims:

```bash
uv run python scripts/provision_identity.py --subject <sub> --user-id ava_moore_2033 --role customer
uv run python scripts/provision_identity.py --subject <sub> --show
```

There is no HTTP route to provisioning: a caller cannot create an account or pick a role.
`/health` reports which mode is live.

Human handoff (M4): a transfer opens a ticket and suspends the customer's session until a
person closes it. A supervisor assigns the case and decides the escalations raised against
it; the assignee works it in a session of their own. A supervisor cannot assign a case to
themselves, nobody decides their own escalation, and a decision expires after
`RSA_APPROVAL_TTL_SECONDS` (30 minutes by default). `GET /session/{id}/timing` reports
system time and human waiting as separate numbers.

Keys are read from `.env` (see `.env.example`). `.env` is gitignored and must not be
committed.

## Start and stop

| Environment | Start | Stop | Status |
|---|---|---|---|
| local | `scripts/start-local.ps1` | `scripts/stop-local.ps1` | implemented and verified by running it |
| AWS | GitHub Actions: `Start Demo` | GitHub Actions: `Stop Demo` | planned, M6 |

The local scripts control only processes this project recorded, verified by PID and
start time, and never guess which python belongs to the project. There is no hosted
demo. The contract is in [operations](docs/OPERATIONS.md).

## Benchmark attribution

Uses [Sierra τ²-bench Retail](https://github.com/sierra-research/tau2-bench), pinned to
`v1.0.1` / commit `fc0055dc4e0a316c3f83133267fbd6faaa770992` (MIT, Copyright (c) 2025
Sierra Research), with per-file SHA256 in `evaluation/tau2_manifest.json`. Upstream
attribution is preserved, and upstream data, tools and evaluators must not be presented
as original work. Standard benchmark scores and this project's own permission, approval
and recovery tests are reported separately.
