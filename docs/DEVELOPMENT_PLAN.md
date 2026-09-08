# MVP and staged development

## Staging principle

Implement and freeze M0 first, then develop one theme at a time. Before each step,
write down the problem, the change, the expected benefit, the possible cost, the tests
that apply and the comparison method. "A component was added" is not evidence of
completion until it has been verified.

## M0: minimal working version

### M0.1 Freeze the protocol and environment

- Pin the τ²-bench commit, licence, Retail data, policy, tools and evaluators, with a
  source inventory and checksums.
- Check that the pinned version actually contains the three kinds of task needed. Do not
  invent task counts or alter the original policy.
- Fix the dev, regression and holdout task lists. Establish the raw result format, the
  run manifest and the comparison report.
- Set up static checks, the minimal relevant tests and CI. There is no MVP performance
  baseline at this point, and that must be reported as "not applicable".

### M0.2 Model interface and tool adaptation

- Connect the OpenAI SDK to the given endpoint and verify each candidate's exact model id.
- Verify tool requests, arguments, tool-result round trips, multi-turn conversation,
  streaming usage, timeouts and truncation.
- Verify the thinking-parameter capability table, and establish one explicitly
  configured reference rather than defaulting to maximum thinking.
- Give every evaluation task and local session its own environment. Sharing a mutable
  upstream database would let them contaminate each other.

### M0.3 Minimal business loop

Look up the order, ask for missing details, apply policy, obtain explicit confirmation,
validate, execute, and report the actual result.

Implemented with Python, LangGraph, FastAPI and a minimal Gradio UI. It uses a fixed
simulated identity, binds to localhost only, and offers no public registration. The data
is τ²-bench simulated business data, connected to no real system.

Baseline requirements: order ownership, tool arguments and policy checks; write
confirmation bound to specific arguments; a step limit, a timeout and stop conditions.
One session is processed serially, and M0 claims neither cross-process idempotency nor
production recovery.

A standard τ²-bench run follows upstream's identification protocol, while the
application demo uses a fixed identity context. The two are recorded separately, and the
demo's identity shortcut must never be used to change standard scoring conditions.

### M0.4 Model and thinking-parameter experiments

Follow [model experiments](MODEL_EXPERIMENTS.md): capability checks, dev-set selection,
level-by-level comparison and independent verification. Keep the business implementation
and evaluator identical throughout, and keep the rejected configurations and failed
results.

### M0.5 Freeze the MVP

- Core tests pass and the quality floor set in advance is met.
- Independent verification is complete and supports the selection. If it does not,
  continue sampling or report what is blocking.
- One model and one thinking configuration are fixed. Dynamic model or budget routing is
  not part of the MVP.
- Raw results, configuration, dependencies, source and image are traceable, and Langfuse
  traces correlate with results.
- Local start, stop and the three demo flows are reproducible.
- `baseline-v1` is archived with checksums, and after the M0 report is delivered, later
  stages do not start automatically.

> **2026-09-07: M0 is frozen.** All six acceptance criteria are met and the
> [M0 report](M0_REPORT.md) has been delivered. The baseline is `baseline-v2` (all 114
> tasks, 0.8509), with `baseline-v1` (40-task holdout) archived. M0.6 later added
> `baseline-v3` for this project's own agent (102/114, 0 unconfirmed writes).
> Per the last line of this section, later stages proceed one at a time, each with a
> performance comparison against the baseline.

## Roadmap

| Stage | Theme developed on its own | Acceptance for the new capability | Performance comparison |
|---|---|---|---|
| M1 | PostgreSQL, migrations, transactions, idempotency | Retries and concurrency do not repeat a modification; failures roll back | Database read/write overhead, success rate |
| M2 | Checkpoints, task claiming, recovery | Recovers after a restart without re-executing | Recovery time, latency of the normal path |
| M3 | Cognito, roles and resource permissions | No privilege escalation; token and ownership checks | Authentication latency and the existing success rate |
| M4 | Assignment, approval, handoff | Correct escalation, refusal and continuation | System time and human waiting reported separately |
| M5 | Performance, model and thinking optimization | One theme optimized at a time | Quality, latency and cost on the same tasks |
| M6 | AWS, Terraform, one-command start/stop | Creation, login, streaming, archiving, teardown, rollback | A separate AWS baseline |
| M7 | UI and demo completion | User actions, state and error display | No regression in the existing backend; interaction accepted separately |

A stage may be subdivided, but every step uses the
[change report template](CHANGE_REPORT_TEMPLATE.md). A new feature is accepted on the
capability it adds and is not required to invent a cost reduction, and nothing is
released without a comparison report.
