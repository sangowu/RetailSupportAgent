# M4: tickets, assignment, approval and human handoff

## 0. Why this step exists

`transfer_to_human_agents` is already recognised: `agent.py` sees the call, ends the turn
and replies "Handed off to a human agent." Nothing else happens. Four consequences:

1. **The handoff goes nowhere.** No record is created, so no human can find the
   conversation, and nobody is responsible for it. The sentence is the entire mechanism.
2. **The session keeps going.** The next user message runs the model again as if the
   handoff never happened, and the agent may attempt the very action it just escalated.
3. **`supervisor` and `agent` are still names.** M3 gave `supervisor` the audit read and
   the global reset, and `agent` nothing at all over `customer`, because acting on
   another customer's order needs an assignment that does not exist. The role matrix in
   `docs/ARCHITECTURE_AND_ACCESS.md` stops being a claim only when an assignment does.
4. **Approval is unimplemented.** The architecture document specifies that an escalation
   is a human decision, that a refusal stops the original action, that approval does not
   grant a policy breach, and that a pending approval expires. None of it exists.

M4 solves those four and nothing else.

## 1. Change card

- **Stage and step**: M4
- **Type**: feature
- **The specific problem**: a handoff produces a sentence rather than a record, the
  session continues as though nothing happened, and there is no way for a human to take
  a case, decide it, or be told that their decision expired
- **The single theme of this change**: a handoff creates a ticket that a human owns and
  decides, and the decision - approve, reject, or let it expire - governs whether the
  escalated action ever runs
- **Control variables this change does not touch**: the model under test, the simulated
  user, the judge, the task sets, the upstream commit, the price snapshot, the M1 schema,
  the M2 claim and the M3 verifier. In particular **`bench_agent.py` is not touched and
  `WriteGuard`'s treatment of `transfer_to_human_agents` does not change**: the benchmark
  path treats it as an ordinary read tool, and adding a permission or a ticket write
  there would alter what upstream measures. Ticketing lives in the application path only.
- **Explicitly out of scope**:
  - **No human interface.** Tickets are created, assigned and decided over the existing
    API. A queue screen for supervisors is M7.
  - **No notifications.** Nothing emails, pages or pushes. A human finds a ticket by
    listing tickets.
  - **No SLA, priority or routing rules.** A supervisor assigns; nothing assigns
    automatically. Inventing a routing policy here would mean inventing a business.
  - **No refund approval thresholds by amount.** The MVP moves no real money, as the
    architecture document already records.
  - **The τ²-bench simulated order data still stays out of PostgreSQL**, for the reason
    M1 gave.
- **Expected benefit**: an escalation becomes a durable record with an owner and an
  outcome; a refusal actually stops the action; an agent can work a case that was
  assigned to them, and only that case
- **Possible cost**: one ticket read per turn on a session that has an open ticket; two
  writes per handoff; a larger surface for the guard to consult when deciding ownership

### 1.1 Design decisions fixed before the work

**A handoff suspends the session; it does not end it.** While a ticket is open the agent
answers that a human is handling the case and runs no tools. The alternative - letting
the model keep working - is what produces the failure this step exists to prevent: the
agent escalating an action and then performing it anyway. Resolution resumes the session
with its state intact, which M2 already guarantees.

**An approval authorizes one argument set, not an action type.** It reuses the guard's
fingerprint, exactly as a confirmation does. Approving "cancel #W001" must not authorize
"cancel #W002", and changing an argument must invalidate the approval rather than silently
carry it over.

**Approval is re-checked at use, not trusted from the past.** The architecture document
requires that even after approval the action re-verify the current order state, the
customer's confirmation, resource ownership and the current role. An approval is one more
condition, never a replacement for the others.

**A pending approval expires.** Default 30 minutes, configurable, and enforced by the
database clock rather than the application's, for the same reason M2's lease is. An
expired approval is not a soft warning: the action is refused and must be requested again.

**Nobody decides their own escalation.** The subject that requested an escalation cannot
approve it, and an agent cannot assign a ticket to themselves. Without that rule the
approval step is a formality that adds a row and no control.

**An assignment widens data access, and only while it lasts.** An `agent` may act on the
orders of a customer whose ticket is currently assigned to them - that is what the role is
for - and the moment the assignment ends, so does the access. The check is a database read
per decision, not a value copied into the session, for the reason M3 gave about roles.

**System time and human waiting are measured separately.** A ticket's open interval is
human waiting; everything else is system time. Reporting one total would make a session
look slower the longer a person took to answer, and every latency comparison against
`baseline-v3` would become meaningless.

### 1.2 Primary metric, protective metrics, acceptance thresholds

Fixed before running, per `MODEL_EXPERIMENTS.md`.

| Metric | Threshold | If not met |
|---|---|---|
| Escalation | A handoff creates exactly one open ticket carrying the session, the requester and the summary | FAIL |
| Suspension | While a ticket is open the session runs no tools and calls no model | FAIL |
| Refusal | A rejected escalation leaves the action unexecuted: no `operations` row, no business change | FAIL |
| Continuation | After a resolution the session resumes with its conversation, counters and confirmations intact | FAIL |
| Expiry | An approval older than its TTL authorizes nothing, and the action must be requested again | FAIL |
| No self-decision | The requester cannot approve their own escalation; an agent cannot assign a ticket to themselves | FAIL |
| Assignment scope | An agent may act on the assigned customer's orders and no others; ending the assignment ends the access on the next request | FAIL |
| Approval is not a bypass | An approved action that violates policy, ownership or the pending confirmation is still refused | FAIL |
| Timing split | System time and human waiting are reported as separate numbers, measured | Explain the cause |
| Degradation | Without a database, handoff behaves as it does today (reply and stop) | FAIL |
| Evaluation score | No regression against `baseline-v3` | Investigate before proceeding |

### 1.3 Baseline and evaluation protocol

- Baseline: `baseline-v3` (this project's agent, 114 tasks, 102/114), protocol
  `evaluation/protocol.md` at the pinned upstream commit
  `fc0055dc4e0a316c3f83133267fbd6faaa770992`.
- The evaluation loop drives `bench_agent.py`, which this step does not modify. As in M2
  and M3, the expected result for the last row is "not applicable", and section 3 must
  confirm that by inspection rather than assume it - here specifically by showing that
  the benchmark path's treatment of `transfer_to_human_agents` is byte-for-byte the
  behaviour that produced `baseline-v3`.

## 2. Implementation

| File | Responsibility |
|---|---|
| `src/retail_support_agent/db.py` | `tickets`, `ticket_assignments`, `approvals`, `sessions.ticket_id` |
| `migrations/versions/0004_tickets.py` | The migration for all four, with a working `downgrade` |
| `src/retail_support_agent/store.py` | Tickets, assignment history, approvals with expiry, and the timing split |
| `src/retail_support_agent/guardrails.py` | `NEEDS_APPROVAL`, checked after ownership and before the confirmation |
| `src/retail_support_agent/agent.py` | Handoff creates a ticket; a suspended session runs nothing; `apply_context` |
| `src/retail_support_agent/api.py` | `/tickets`, assign, close, work, `/approvals`, decision, `/session/{id}/timing` |
| `src/retail_support_agent/auth.py` | Four new permissions and the roles that hold them |
| `tests/test_handoff.py` | 62 tests, one class per acceptance threshold |

The life of a case:

```text
customer session   -> transfer_to_human_agents -> ticket (open)   [session suspended]
supervisor         -> assign to an agent       -> ticket (assigned)
agent              -> own session bound to the ticket
agent's write      -> guard: NEEDS_APPROVAL    -> approval (pending, expires in 30 min)
supervisor         -> approve or reject        -> approval (approved | rejected)
agent's write      -> guard: ownership, approval, confirmation -> executed once (M1)
agent              -> close                    -> ticket (resolved | rejected)
customer session   -> runs again
```

### 2.1 Four powers, three roles

| | customer | agent | supervisor |
|---|---|---|---|
| `order:read` / `order:write` | own | own, and the assigned customer's | own |
| `ticket:read` | - | own queue | all |
| `ticket:work` | - | yes | **no** |
| `ticket:assign` | - | - | yes |
| `escalation:decide` | - | - | yes |

`supervisor` deliberately lacks `ticket:work`. An approver who also performs the action
is not an approver, which is the same rule as "a supervisor cannot confirm on a
customer's behalf". `/tickets` narrows itself by role rather than trusting the client to
ask for its own queue, because "an agent cannot widen their own data visibility" is a
rule and not a default view.

### 2.2 What the agent's session is, and is not

An agent works a case in a session of their own, keyed by the ticket and their subject.
It is not the customer's session: that conversation belongs to the customer, and M3's
ownership check says so. `sessions.ticket_id` is what makes "acting for someone else" a
stored fact, so the customer it acts for comes from the ticket rather than from a request
field, and the assignment is re-read on every turn - reassigning a case ends the previous
agent's access on their next request, not when the process next restarts.

## 3. Results

Verified against PostgreSQL 17 (`compose.yaml`, loopback only) with
`RSA_TEST_DATABASE_URL` set. All 62 new tests executed rather than skipped; the whole
suite is 448 tests passing, against 386 before this change. The tests needing a database
refuse to skip under `CI`.

### 3.1 The acceptance thresholds

| Metric | Threshold | Measured | Result |
|---|---|---|---|
| Escalation | Exactly one open ticket, carrying session, requester and summary | ticket created with all three; a second transfer returns the same ticket | pass |
| Suspension | No tools, no model call while a ticket is open | the scripted model records 0 calls; the customer's message is still journaled | pass |
| Refusal | A rejected escalation leaves the action unexecuted | no approved fingerprint, no `operations` row, the guard asks again | pass |
| Continuation | The session resumes with its state intact | runs again after close; steps 7 / tool calls 5 preserved across the wait | pass |
| Expiry | An expired decision authorizes nothing | expired approval yields no fingerprint; deciding one returns `expired`; not listed as pending | pass |
| No self-decision | The requester cannot decide; a supervisor cannot self-assign | refused in the store and answered 403 by the API | pass |
| Assignment scope | Only the assigned customer, and only while assigned | reassignment refuses the previous agent's next request with 403 | pass |
| Approval is not a bypass | Still refused by every other check | missing confirmation, another user's order, different arguments, and a role without the write are each still refused | pass |
| Timing split | Two numbers, measured | `system_seconds` and `waiting_for_human_seconds`, overlapping waits counted once | pass |
| Degradation | Without a database, M0 handoff behaviour | reply "Handed off to a human agent.", status `answered` | pass |
| Evaluation score | No regression against `baseline-v3` | not applicable; see 3.4 | see 3.4 |

### 3.2 The refusal is the point of the step

Three tests carry it, because "the human said no" has to mean the action never happens
rather than the model being told to feel bad about it:

- A rejected request leaves no approved fingerprint, so the guard returns
  `NEEDS_APPROVAL` again rather than letting the call through.
- No `operations` row exists for the session, which is M1's record of what actually ran.
- A rejected request cannot be decided a second time into an approval.

The positive control sits next to them: an approved request for the same arguments does
let the call through. Without it, a version of this code that refused everything would
pass all three.

### 3.3 What M4 costs

| Operation | Measured |
|---|---|
| `open_ticket_for` (suspension check, every turn) | median 2.6 ms, p95 3.4 ms |
| `session_ticket` (every turn) | median 1.8 ms, p95 2.6 ms |
| `assignee_of` (every turn on an agent session) | median 1.8 ms, p95 2.2 ms |
| `approved_fingerprints` (every turn on an agent session) | median 2.0 ms, p95 2.4 ms |
| `open_ticket` (once per handoff) | median 2.7 ms, p95 3.3 ms |
| `session_timing` | median 3.6 ms, p95 4.5 ms |

A customer's turn pays about 4.4 ms more than under M3 (the suspension check plus the
ticket lookup). An agent's turn pays about 8 ms, because it also re-reads the assignment
and the approvals. Both are database reads against a model call measured in seconds.

Everything is re-read rather than cached in the session, which is the deliberate choice:
a released assignment or an expired approval has to stop working on the next request, and
a cached copy would keep working until the process restarted.

### 3.4 Not verified

- **No regression in evaluation scores.** The evaluation loop drives `bench_agent.py`,
  and `git diff` shows that file, `evaluation/` and `scripts/run_eval.py` are untouched
  by this change. Two tests pin the two ways M4 could have leaked into it:
  `transfer_to_human_agents` is still not in `WRITE_TOOLS` and still passes the guard as
  an ordinary read, and `SessionState.acting_for_other` defaults to False, so the
  benchmark path reaches exactly the M3 verdicts. No run was made, and none is claimed.
- **A human actually using this.** There is no queue screen, no notification and no
  routing. A supervisor finds work by listing tickets. The interface is M7.
- **Handoff triggers beyond an explicit transfer.** The architecture document also names
  "the agent cannot resolve the request within its allowed tools and steps" and
  "business information conflicts" as reasons to create a handoff record. Only the
  explicit `transfer_to_human_agents` call creates one here. A session that hits its step
  limit still stops with `stopped` and no ticket.
- **Concurrent decisions on one approval.** Two supervisors deciding the same request at
  the same instant is not tested. The second one reads `already_decided` in the same
  transaction that would change the row, which makes the race narrow, but narrow is not
  the same as tested.
- **Cross-session confirmation.** An agent's write needs the customer's confirmation in
  the agent's own session. Carrying a confirmation across from the customer's session is
  not implemented: a fingerprint the customer approved while talking to the model is not
  evidence that they approved the agent doing it later.
- **Notification of expiry.** Nothing tells the requester that their request expired.
  They find out when the action is refused again.

### 3.5 What went wrong along the way

`TurnResult.status` was a closed `Literal`, and adding the three new outcomes -
`handed_off`, `waiting_for_human`, `needs_approval` - failed type checking until each was
declared. That is the type doing its job: every one of them is a state a caller has to
handle differently, and quietly reporting them as `answered` would have been the easy
wrong answer.

The M3 test file's key material and token minting are reused rather than copied. A second
definition of "a valid token" would drift from the verifier the moment either changed.

## 4. Conclusion

**PASS.** Ten of the eleven thresholds are met and measured; the eleventh, no regression
in evaluation scores, does not apply because the evaluation loop does not construct the
service and its files are untouched, which is stated rather than reported as a pass.

What this step adds, in one line: a handoff now produces a case a human owns, decides and
closes, and the decision - including a refusal, and including one that expires - governs
whether the escalated action ever runs.

What it leaves open is M4's own boundary rather than a defect: nobody has a screen to work
these tickets on, and until M7 gives them one, the queue is a list of JSON objects.
