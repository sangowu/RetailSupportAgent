# Project goals and decisions

Recorded 2026-09-06; updated as later stages landed. This file records the requirements
confirmed in discussion. It does not assert that a feature already exists.

## Business goal

Given a simulated retail user request, the agent looks up an order, cancels one, or
starts a return, within policy and its authorization. Modifying operations require
explicit confirmation, and when something cannot be handled the agent says why or hands
off to a human. Success is judged by the correct final business state together with the
required confirmations and policy compliance, not by whether the reply claims success.

The first version excludes RAG, multiple agents, real payments, real refunds and a
production order system. An accepted return request is not a refund that has landed,
and answers must match the simulated state the tools returned.

## Decisions confirmed by the user

| Item | Decision |
|---|---|
| Method | MVP first, then freeze a baseline; develop stepwise with a performance comparison at every step |
| Business data | τ²-bench Retail, pinned version, simulated business |
| Evaluation scope | Run all 114 tasks per upstream's design with all 16 tools exposed; report the 42-task subset covering the three declared flows separately (confirmed 2026-09-06) |
| Agent | LangGraph, single agent |
| Model interface | OpenAI Python SDK, Chat Completions compatible |
| base_url | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Original reference model | `qwen3.7-max-2026-06-08` |
| Added candidates | `qwen3.8-max`, `deepseek-v4-pro-0813`, `glm-5.2`, `qwen3.8-flash` |
| Final model | Decided by this project's tests; no winner named in advance |
| Thinking configuration | Verify which parameters are supported, test thinking mode and budget, choose the lowest sufficient setting rather than defaulting to the maximum |
| Authentication | Cognito user pool, a later stage |
| Permission rules | Drafted by the developer as explicit, testable rules |
| AI observability | Langfuse cloud |
| CI/CD | GitHub Actions; a release gate does not pass without a comparison report |
| Gate exceptions | A fix passes on regression verification and maintenance on no regression; neither is required to fabricate an improvement |
| AWS | Created on demand for a demonstration and cleaned up afterwards; managed with Terraform |
| Public URL | A free URL; the CloudFront default domain is a candidate still to be verified |
| Start and stop | A single entry point plus first-time setup instructions, for both local and AWS |
| Budget | No spending cap set by the user; this is not authorization to start billing models or deploying cloud resources |

## Technology and stages

| Technology | Purpose | Stage |
|---|---|---|
| Python, uv, lockfile | Program and reproducible dependencies | M0 |
| FastAPI, Uvicorn, Pydantic | API and validation | M0 |
| OpenAI SDK, LangGraph | Model calls and business flow | M0 |
| Gradio | Minimal demo UI | M0; completed in M7 |
| Langfuse cloud | Calls, experiments, usage and latency | M0 |
| Ruff, mypy, pytest, pytest-asyncio, HTTPX | Static checks, async and API tests | From M0 as needed |
| Docker, Docker Compose | Reproducible execution | From M0 |
| PostgreSQL, SQLAlchemy, psycopg, Alembic | Application persistence, transactions and migrations | M1 |
| LangGraph persistent checkpoints | Interruption and recovery | M2 |
| Cognito | Authentication | M3 |
| GitHub Actions | CI and evaluation gates; CD later | From M0 |
| Terraform, ECR, ECS Fargate, RDS, ALB, Secrets Manager, S3, CloudWatch, IAM OIDC | On-demand cloud demo | M6 |

## Evidence of success

The project must provide: a reproducible way to start it, per-task results on a fixed
task set, the reasoning behind the model and thinking configuration, a comparison report
for every development step, and real failure cases with evidence of their fixes. A
public benchmark verifies capability in a simulated setting; it is not customer value
and not an absence of production risk.

The development protocol and metric thresholds are drafted by the developer and locked
before any later candidate's results are seen. A provisional number in a document must
never be presented as a measurement.

## Facts still to be established by implementation

- Whether the five exact model ids are available on the given account and endpoint, and
  whether the parameters actually take effect.
- Task classification, licensing, tools, simulated-user protocol and scoring semantics
  in the pinned τ²-bench version.
- Whether the sample size supports conclusions about success rate, P95 and non-regression.
- Whether a free HTTPS entry point is compatible with Cognito callbacks, streaming
  responses and origin protection.
- Real AWS creation and teardown times, resource cost, and a check for leftovers.

These are open questions, not answers to be invented in advance. When an interface turns
out to be unsupported, report the fact rather than quietly substituting a different
model or provider.
