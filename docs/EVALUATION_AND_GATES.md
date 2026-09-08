# Evaluation, baselines and CI/CD gates

## Immutable references

- `baseline-v1`: the first snapshot of source, parameters, environment and results that
  passed M0 acceptance. Kept permanently.
- `last-accepted`: the previous passing version. The pointer moves; its historical
  artifacts are never overwritten.
- A candidate reports against both. Within M0 the default reference is kept while
  parameter experiments run, and no MVP baseline is claimed at that point.
- Every step needs a report. Steps with no runtime change, such as documentation or
  maintenance, still record a static impact check and comparison evidence. A trustworthy
  score may be reused from the same image and inputs, provided the reason is stated and
  it is not presented as a new run.

## Splits and scoring

1. Pin the τ²-bench version and official splits, keeping the original policy, the
   simulated-user semantics and the evaluators.
2. A score on the three-flow subset must be labelled as this project's subset and never
   presented as a complete Retail benchmark score.
3. The dev set is for debugging and selection, the regression set for defect and release
   checks, and the holdout for stage acceptance.
4. Permission, approval, transaction and recovery scenarios built by this project keep
   their own expected state and explicit assertions.
5. Reference answers, expected actions, scoring criteria and hidden user instructions
   must not leak to the agent under test.
6. A reference action sequence may be only one path to the target state. Keep upstream's
   scoring semantics rather than forcing a reasonable alternative path to match step by
   step. This project's safety assertions run separately.

Upstream sources: [τ²-bench](https://github.com/sierra-research/tau2-bench),
[domain and scoring structure](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/domains/README.md).

## Metric definitions

| Metric | Definition |
|---|---|
| Task success rate (primary) | Runs meeting the task's scoring conditions divided by all valid attempts, reported separately for lookup, cancel, return and by difficulty |
| Safety violations | Count and denominator of privilege escalation, unconfirmed writes, duplicate operations and policy breaches; hard scenarios must be zero |
| Repeat stability | Repetitions per task, successes, and the share of tasks that succeeded every time; every result kept |
| Tool reliability | Wrong arguments, execution errors, timeouts, retries, invalid or duplicate calls |
| Human handoff | Recall on scenarios that should hand off, and the rate of unnecessary handoffs; scored only once the feature exists |
| Recovery rate | Share of recoverable failures that recovered correctly with no duplicated side effect |
| First-response latency | From request to the first useful reply the user can see; an internal reasoning chunk is not a reply |
| Task latency | System processing time to the final state, P50 and P95; human and simulated-user waiting reported separately |
| Cost | Total cost per attempt, and total cost per success, the latter including the cost of failed attempts; with zero successes it is not recorded as 0 |
| Usage | Input, output, thinking and cached tokens, LLM and tool call counts; unknown fields marked unavailable |
| Engineering checks | Lint, type checking, tests and coverage of key branches; coverage does not substitute for behavioural assertions |

Thinking tokens may already be included in output billing and must not be charged
twice. Agent, simulated-user and judge costs are separate, with the experiment total
reported as well. `scripts/analyze_eval.py` is the single authority for cost, using the
dated snapshot in code. The Langfuse dashboard shows no cost: the usage it receives has
no cache breakdown, and full-price arithmetic overstates cost about 5x (measured 0.82
CNY against a real 0.16 CNY), so giving it a price table would make the dashboard report
an unverified number. See [M0.5 observability](M0.5_OBSERVABILITY.md). The 0 in the
dashboard is a known gap, marked as "not free" in summary.json and the run log, and must
never be recorded as free.

## Comparability of runs

- Fix the runner spec, region, concurrency, data, cache handling, model and
  simulated-user parameters, timeouts and retries.
- Interleave baseline and candidate runs and record every run and provider anomaly. A
  candidate's current latency cannot be compared against a network environment from
  months ago.
- Reset the environment per task, and never treat repeated runs of one task as extra
  independent samples.
- Use the task as the unit for paired analysis and bootstrap intervals, keeping the
  random seed and the version of the statistics script.
- Fix the repetition count and the rule for extending the sample. Re-running until
  significance is forbidden. A service failure re-runs the whole group and keeps the
  original record.
- Local and AWS, different hardware and different cache modes each get their own
  performance reference; quality differences and migration differences are explained
  separately.
- A fixed model configuration still has server-side randomness: temperature=0 is not
  determinism.

## Thresholds and statistics

These are a design starting point, not measurements. M0 first estimates variance and the
required sample size on the dev set, then freezes the protocol. Thresholds are never
changed after seeing a candidate's results. Record an absolute quality floor rather than
only proving no regression against a poor reference. The floor is written into the run
configuration as an explicit value after the M0 task inventory, and the gate does not go
live before it is frozen.

| Item | Initial agreement |
|---|---|
| Hard tests | Every applicable permission, confirmation, transaction, idempotency and recovery assertion passes; any failure blocks release |
| Quality non-regression | The one-sided 95% lower bound on the success-rate difference is not below -3 percentage points, and a breakdown is not hidden by the overall average |
| Cost and latency protection | The one-sided 95% upper bound on the ratio of non-target cost and P95 does not exceed 1.10 |
| Cost and latency optimization | A pre-declared target improves by at least 10% at the point estimate, with a 95% interval supporting a real improvement |
| Quality optimization | A pre-declared success rate improves by at least 5 percentage points at the point estimate, with a 95% interval supporting a real improvement |
| Insufficient evidence | `INCONCLUSIVE`; nothing is merged or released automatically |

Interval judgements at 3 percentage points and on P95 may need a large sample. Thirty
tasks or three repetitions cannot be declared sufficient in advance. Screening and
formal acceptance are separate; extend the sample per a pre-registered plan and report
honestly if it is still insufficient. Multiple candidates are screened on the dev set
and confirmed on independent data, so the best-looking significant result cannot be
picked after the fact.

## Release conditions by change type

| Type | Conditions |
|---|---|
| Feature | The new capability passes the applicable tests, and existing business and protective metrics meet non-regression |
| Optimization | At least one pre-declared target improves clearly, with all hard checks and protective metrics passing |
| Fix | A new regression test proves the defect is fixed, and existing metrics do not materially regress |
| Maintenance / UI | Behaviour acceptance and evidence of no regression; no fabricated performance improvement required |

If the necessary overhead of a feature exceeds a threshold, it is not quietly exempted:
it becomes an explicit trade-off decision and a new protocol version, after which both
references are re-run. Failures and inconclusive results are kept indefinitely.

## CI/CD layers

1. Per PR: Ruff, mypy, unit and API tests, the applicable PostgreSQL integration and
   permission tests, and a build check.
2. Fast performance: fixed simulated tool and model replay to check local scheduling
   overhead. This is not an evaluation of real agent capability.
3. Real model evaluation: triggered manually, bound to a candidate commit, image and
   protocol. Changes to the model, prompt, tools or orchestration must complete this
   layer before a release gate can pass.
4. Aggregation: read the trusted protocol and baselines and produce PASS / FAIL /
   INCONCLUSIVE. A required check must not be skipped because a path filter excluded it.
5. Release: only the verified image digest bound in the report. Re-verify when a merge
   produces new run content. A successful deploy still needs health and smoke checks.

GitHub branch and ruleset capabilities depend on the account plan and must be verified.
Where they are unavailable, use an explicit release process rather than claiming the
platform enforces protection. Changes to the protocol, baselines or evaluators need
dedicated review, and a candidate branch cannot relax its own standards. Deploying
directly after a failure to bypass a gate is forbidden.

## Archiving

M0 produces the manifest, checksums, per-task results, summary, run logs and the
Langfuse session id locally; the AWS stage copies them to S3 and verifies them. The
baselines are frozen under `evaluation/baselines/` (not the originally suggested
`artifacts/` prefix; see [M0.5 part three §5](M0.5_BASELINE.md)), and per-run artifacts
live in `evaluation/results/<run-id>/`. Checksums are split into committed files and
local raw traces, and CI verifies the former. Raw per-task traces are not yet copied to
S3: their digests are recorded, so loss would be detected but not recoverable.

The first baseline, the current release and any needed rollback image are kept, and a
temporary-build cleanup rule must not remove them. Dataset or evaluator errors are
corrected in a new version while the old results are kept and marked invalid with the
reason, never rewritten silently.
