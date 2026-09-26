# Delivery report: M1 to M8

**RetailSupportAgent** - a retail support agent (order lookup, order cancellation, return
request) built to prove business correctness, reliability and cost through reproducible
evaluation.

Report date: 2026-09-10, carried through M8 on 2026-09-25. Scope: M1 to M8. M0 has its
own report and is not restated here; where a number comes from M0 it is marked. M8 has two
documents of its own, [the access measurement](M8_ACCESS_EVAL.md) and
[the release record](M8_RELEASE.md); this report states what they establish and does not
restate them.

---

## 1. Conclusions on one page

| | |
|---|---|
| Subject under test | this project's guarded agent, frozen as `baseline-v3` |
| Score | **0.8947 (102/114)** on Sierra tau2-bench Retail v1.0.1, all 13 files verified by SHA256 |
| What the guard changed | writes executed without confirmation: **168 to 0**, and the score did not fall (McNemar p = 0.3018) |
| What the guard cost | +16% model calls, +12% money: 0.0954 CNY per task against upstream's 0.0855 |
| Latency | agent call p50 2.86s, p95 15.30s; 59% of a task's wall clock is inside agent calls |
| State | PostgreSQL, migrations, idempotent writes, recovery across a process restart |
| Authorization | read from the database, never from the token; a refusal names the permission it wanted |
| Human handoff | a case is escalated, assigned, worked in the agent's own session, held for approval, decided, closed |
| Deployment | one instance behind one name over TLS, stopped when idle: 1.38 USD a month stopped, about 16 running |
| Version changes | one parameter and a restart, measured across a real behavioural change |
| Access control | **22/22 blocked** on the `access-v2` adversarial set: cross-user reads and writes, enumeration, fabrication, prompt injection |
| Releases | eight images, `m8` to `m8.7`, each published and verified against the running deployment; **fifteen defects**, thirteen of them found by a person using it |
| Engineering | **1051 tests** (275 at the end of M0), ruff and mypy clean, and CI has run the suite on every merge since the repair in section 7 |

**The claim this project makes is not that the agent works, but that each thing it does can
be shown to work and priced.** So every number below names the run or the report it comes
from, and section 6 says what is not covered.

---

## 2. What each stage established

| Stage | What it added | The evidence |
|---|---|---|
| [M1](M1_PERSISTENCE.md) | application state in PostgreSQL, with migrations | one key repeated 5 times executes once; 8 concurrent submissions execute once; a failure leaves neither the operation record nor its effect |
| [M2](M2_RECOVERY.md) | session claim, replay, restored limits | recovery reads are flat from 1 to 50 operations, because a replayed write is 0.011 ms |
| [M3](M3_AUTH.md) | Cognito tokens, roles and permissions from the database | signature verification is 0.035 ms; the cost is the two database reads, about 6.5 ms a turn |
| [M4](M4_HANDOFF.md) | tickets, assignment, approval, handoff | a customer's turn pays 4.4 ms more, an agent's 8 ms, and every check is re-read rather than cached |
| [M5.1](M5.1_MEASUREMENT.md) | cost priced by the run's clock, latency as a distribution | 26 of 26 historical runs re-analyze; the first three-axis comparison |
| [M5.2](M5.2_THINKING_EFFORT.md) | what the task set needs from thinking | both candidates rejected, one for buying nothing, one for six wrong business outcomes |
| [M5.3](M5.3_THINKING_ROUTING.md) | whether thinking can be routed per turn | it cannot mid-turn, measured; the legal alternative has a 24% ceiling and zero effect on p95 |
| [M6.1](M6.1_DEPLOYMENT_SHAPE.md) | the deployment shape, chosen from measurement | one instance stopped when idle; the zero-idle-cost shape needed a rewrite or a 35 USD NAT gateway |
| [M6.2](M6.2_CONTAINER_AND_START.md) | the image, and whether it starts in time | a restart answers in 41 s against a 120 s budget; a first creation takes 122 s |
| [M6.3](M6.3_IDLE_SHUTDOWN.md) | the instance stops itself | a demo left running costs about 16 USD a month, against the volume alone when stopped |
| [M6.4](M6.4_TERRAFORM.md) | the environment as code | no number of its own; see below |
| [M6.5](M6.5_START_STOP.md) | two commands an operator runs | start in 29 s, stop refuses while work is unfinished |
| [M6.6](M6.6_HTTPS.md) | one name, over TLS, surviving a stop | same certificate serial across a stop, new address, same URL, 66 s |
| [M6.7](M6.7_LOGIN.md) | a login in front of it | every write route answers 401 anonymously, 403 to an account with no row here |
| [M6.8](M6.8_ROLLBACK.md) | a version change without rebuilding the machine | one parameter and a restart; measured again in section 3.3 across a real change |
| [M6.9](M6.9_ARCHIVE.md) | the record outlives the machine | an archive restored onto a destroyed volume, all twelve tables |
| [M7.1](M7.1_STREAMING.md) | the turn is visible while it runs | first event at 0.17 s where the panel was blank for 8.80 s |
| [M7.2](M7.2_STATES.md) | every state says what it is and what to do | seven statuses and four failures, each rendered from the type rather than from a list |
| [M7.3](M7.3_TICKETS.md) | the case work is reachable | a case taken end to end through three roles, and the account that could do it did not exist before |
| [M8](M8_ACCESS_EVAL.md) | the access controls, measured rather than asserted | 22/22 blocked, and writing the set found a refusal the audit trail never recorded - each of the three had a passing unit test |
| [M8 releases](M8_RELEASE.md) | the controls put where a person can reach them | eight releases, and fifteen defects that only using the thing could find |

M6.4 has no number of its own. It converted a working environment into one that can be
recreated, and its result is that every step after it is an `apply` rather than a console.

---

## 3. The numbers

### 3.1 Evaluation

Two subjects, never averaged, both on the same 114 tasks and the same frozen stack.

| | Upstream `llm_agent` (`baseline-v2`) | This project's guarded agent (`baseline-v3`) |
|---|---|---|
| Passed | 97 (0.8509) | **102 (0.8947)** |
| Writes executed without confirmation | 168 | **0** |
| Model calls | 1233 | 1428 (+16%) |
| Three-role cost | 0.0855 CNY per task | 0.0954 CNY per task (+12%) |

Both pass 92 and both fail 7; the guarded side fixes 10 and breaks 5. **McNemar p = 0.3018.**

What that supports: enforcing confirmation did not lower the score. What it does not
support: that the guard raised it. The certain result is in the other row, and it cost 16%
more calls to get.

**No stage after M4 re-ran the evaluation, and each says why.** The benchmark reaches
`agent.py`, `guardrails.py`, `domain.py` and `tau2_pin.py`. Persistence, authentication,
tickets, deployment and the page are not on that path, and `RetailAgent` has been
byte-identical since M7.1. A re-run would have cost about 10 CNY to measure nothing.

### 3.2 What each layer costs a turn

Measured against a model call whose p50 is 2.86 s.

| Layer | Added per turn | From |
|---|---|---|
| Session claim and progress | about 9 ms | M2 |
| Identity and ownership reads | about 6.5 ms | M3 |
| Suspension and ticket lookup (customer) | 4.4 ms | M4 |
| The same plus assignment and approvals (agent) | 8 ms | M4 |

Everything is re-read rather than cached. A released assignment or an expired approval has
to stop working on the next request, and a cached copy would keep working until the process
restarted.

### 3.3 What the deployment costs, and how fast it moves

One `t3.small` in `eu-west-1`, PostgreSQL in a container beside the application. That is a
demo topology and not a production one; the reason is cost and it is written down in M6.1.

| | |
|---|---|
| Stopped, the normal state | 1.38 USD a month: 0.88 for the 10 GiB volume, 0.50 for the hosted zone |
| Left running | about 16 USD a month |
| First creation | 122 s |
| Start from stopped | 29 s before TLS, 66 s with the name and the certificate |
| Restart onto a new version | 85 s and 98 s, measured in both directions |
| Stop | 25 s, and it archives on the way down |

The instance stops itself after fifteen idle minutes, and refuses to stop while a ticket is
open, an approval is pending or a confirmation is outstanding.

**The version change, measured across a real behavioural change.** M6.8 built the mechanism
and could only demonstrate it between two images that differed in a milestone string. The
M7 release put a second real version in the registry, so the round trip was taken:

| | `m7.3` | `m6.8.1` | `m7.3` again |
|---|---|---|---|
| Page bytes served | 214174 | 100050 | 214175 |
| The four case-section labels | 1 each | 0 each | 1 each |
| Restart to reach it | - | 98 s | 85 s |

Instance, volume, launch time, address and certificate serial were read at all three points
and did not change, and three archives counted the same rows in every table. Details and
limits are in M6.8 sections 4.5 and 4.6.

---

## 4. Measured, then rejected or retired

The stages that changed nothing are the ones that took the most measuring.

- **Thinking at `low`** costs 6% more than `high`, returns more reasoning tokens despite
  the lower setting, and changes neither latency nor quality. Rejected for buying nothing.
- **Thinking disabled** takes a third off the cost per successful task and cuts p95 from
  15.30 s to 4.24 s, and gets six business outcomes wrong that the incumbent gets right
  every time. Rejected because the project's claim is business correctness.
- **Routing thinking per turn** is impossible as designed: a conversation may mix thinking
  modes, but not inside a single assistant turn, and the router's decision point is always
  mid-turn. The legal alternative has a ceiling of 24% on cost and exactly zero on p95. The
  probe and the arithmetic were kept so the money is not spent again.
- **"Nobody may decide their own escalation"** was written as an M7.3 threshold and
  retired after measuring. Raising an escalation needs `ticket:work` and deciding one needs
  `escalation:decide`, and no role holds both, so the rule cannot be violated and therefore
  cannot be demonstrated. Retired with that reason rather than reworded into something the
  step happened to satisfy.
- **Shape 3 of the deployment**, the one with no idle cost, needed either a rewrite of
  `store.py` or a NAT gateway at 35 USD a month.

## 5. Defects found by doing the work

Each of these was found by building something else, which is the argument for building it.

| Found in | What it was |
|---|---|
| M1 | `alembic.ini` held non-ASCII comments, and on Windows configparser reads it in the machine's locale encoding |
| M2 | whichever test imported `tau2` first fixed the data directory for the whole process, so the suite passed or failed on file order |
| M3 | `provision_identity.py --revoke --show` printed the identity before applying the revocation |
| M3 | a FastAPI dependency declared inside `create_app` is invisible to annotation resolution, and every request answered 422 |
| M6.8 | the Caddy health check had been failing on every boot since M6.6, and nothing had read the unit's result until `demo.py restart` asked systemd |
| M6.9 | a Systems Manager session inherits none of a unit's environment, so the automatic archive would have worked and the operator's command would have failed every time |
| M6.9 | `status` hid the archive age for a stopped instance, which is the state it is in the minute before a teardown |
| M7.1 | the streaming measurement said streaming did not stream, because fastapi's `TestClient` buffers the response |
| M7.2 | under `from __future__ import annotations`, `__annotations__` yields the string, so an exhaustiveness test over the statuses would have passed over an empty set |
| M7.3 | no account in the demo could work a case: the two customers cannot, and a supervisor deliberately holds no `ticket:work` |
| The M7 release | an instance script corrected after the instance was launched changes the user data, so an untargeted apply plans to replace the machine |
| This report | CI on `main` has failed at the mypy step since M6.3, which skips the test job, so no merge since then has run the suite in CI |
| [M8](M8_ACCESS_EVAL.md) | ownership refusals on the write path recorded nothing, so a block against another customer's order was correct and invisible - and all three had passing unit tests |
| [M8.6](M8_RELEASE.md) | every session kept its own copy of the retail domain in a dictionary that only grew, on a 2 GB instance shared with the database |
| [M8.7](M8_RELEASE.md) | an approved action that was never proposed again was reported to the customer as cancelled and refunded; `operations` was empty |
| A start on non-default ports | `start-local.ps1` takes `-ApiPort` and `-UiPort`, but the UI reads its port and the API address from the environment and nothing set them: the UI bound its own default 7860 while the script waited out the full budget on the port it had been given, then rolled the working API back with it. Both log files were empty, because the process never failed |

Two of them, the health check and the archive through Systems Manager, would have failed
silently in the half nobody watches while the half being demonstrated kept working.

## 6. Explicitly not done

- **A rollback between versions that do not share a schema.** No migration has been added
  since M4, so the round trip in 3.3 carries no downgrade question. That is the case where
  a rollback can genuinely fail, and it is not measured.
- **Nothing is measured under load.** Every number is one conversation at a time. There is
  no figure for concurrent users, and the single instance was not chosen with one in mind.
- **The topology is a demo topology.** PostgreSQL runs on the application instance, in one
  availability zone, with no replica. M6.1 chose that for cost and says so.
- **One model.** Everything is `deepseek-v4-pro` at thinking effort `high`. M0's
  cross-model comparison was discarded because the judge was not held constant, and was
  not redone.
- **An agent cannot find unassigned work.** `GET /tickets` narrows to the caller's own
  assignments for anyone without `ticket:assign`, so an agent's list is empty until a
  supervisor pushes a case. M4's design, surfaced by M7.3, unchanged.

### What M8 closed on this list

Four items above were removed rather than answered in place, because M8 settled them and a
report that still listed them would be wrong:

- **The audit trail has a screen**, and the browser acceptance used it: an `audit:read`
  panel reading a named session, which is how the release record shows a write landing with
  a `write_executed` beside it.
- **The page is measured in a browser.** Assignment, working a case, escalation, closure,
  approval expiry and the streaming states were each walked by a person. Thirteen of M8's
  fifteen defects came from those walks - the other two needed concurrent requests built on
  purpose and a memory measurement - and every user-visible state has now been watched at
  least once.
- **Approval expiry was exercised end to end**, and it was the worst of the four walks. The
  expiry logic was correct throughout; what was wrong was everything the path *said* -
  including a session reporting an order cancelled and a refund on its way when nothing had
  run.
- **CI runs the suite again.** The 49 mypy errors were repaired, and the suite runs on every
  merge rather than only on the machine it was run on; it was 1047 tests at that repair.

The instance drift from the M7 release is also gone from the list, but it was not closed the
way the others were: the `m8.7` release changed the instance's user data, so the apply
replaced the machine and its volume. Nothing was lost - the instance had been stopped, and a
stop archives - and the release record carries it. The rule it recovers was already written
in section 5 of this report, at the M7 release, which is the part worth keeping: the rule
existed and was not consulted.

## 7. Corrections this report makes to the record

Three, all found while checking the numbers rather than while writing them.

**The baseline label has been wrong since M6.7.** Every report from M6.7 to M7.3 carries
"102/114 (`baseline-v2`)". 102/114 is `baseline-v3`, this project's guarded agent;
`baseline-v2` is upstream's `llm_agent` and scores 97/114. M3 and M4 name it correctly, so
the error entered at M6.7 and was copied forward six times. It never changed a number, only
the attribution of one - which is the same mistake M0's correction 1.1 exists to prevent.
Twelve occurrences are corrected in this change.

**README said the AWS demo was planned and that there was no hosted demo.** It has been
deployed since M6.4 and has been reachable over TLS with a login since M6.7. Corrected.

**CI has been red on `main` since M6.3, and this report is what noticed.** mypy is strict
over `src`, `scripts` and `tests`, and the three test files added by the deployment work -
`test_idle_shutdown.py`, `test_deploy_files.py`, `test_demo_commands.py` - carry 49 errors
between them: missing annotations, bare generics, absent stubs for `yaml`. The mypy step
fails, the pytest step is skipped because it depends on it, and **thirteen merges have
landed on `main` without CI running the suite once**.

Locally the 49 were treated as a baseline to return to after each change rather than as a
failure to fix, which is how they survived seven milestones. Nothing was hidden: the runs
are red in the open and every report says the suite passed, which it did, on the machine
it was run on. What was missing is that nobody asked what CI thought.

**Settled.** The [CI test typing repair](CI_TYPE_FIX.md) fixed the 49 errors, and every
merge since has run the three jobs - the evaluation gate, static checks with the suite, and
the upstream pin and domain replay - green before landing. The count was 1047 there and is
1051 now. The paragraphs above describe the original `a31ebb2` revision and are kept,
because what is worth remembering is not the errors but the seven milestones they survived
as a baseline to return to.

## 8. Reproduction

**Deployment maintenance, 2026-09-10:** the
[user-data reconciliation](USER_DATA_RECONCILIATION.md) reproduced the pending instance
replacement with a live full Terraform plan, reconciled launch metadata while the
instance remained stopped, and verified a final full plan with no changes. The instance
and its root volume were preserved. This is deployment-state verification, not a fresh
runtime acceptance run.

```bash
uv sync --group bench
uv run python scripts/fetch_tau2.py                          # the pinned commit, SHA256 verified
uv run ruff check . && uv run mypy && uv run pytest
uv run python scripts/freeze_baseline.py --verify            # the baselines are unaltered
uv run python scripts/run_access_eval.py                     # 22/22 blocked, no model, free

# the database-gated tests need PostgreSQL; without it part of the 1051 skip
docker compose up -d db
uv run alembic upgrade head                                  # RSA_DATABASE_URL must be set

# these spend real money
uv run python scripts/run_eval.py --split full               # 114 tasks, about 10 CNY
uv run python scripts/analyze_eval.py --run <run-directory>

# the deployed demo
uv run python scripts/demo.py status
uv run python scripts/demo.py start
uv run python scripts/demo.py stop
```

Raw artefacts: `evaluation/results/<run-id>/`, `evaluation/baselines/`,
`evaluation/demo_runs/`. Per-step reports are indexed in `README.md`.
