# M2: cross-process recovery (session claim, replay, checkpoints)

## 0. Why this step exists

M1 persists application state but never reads it back to rebuild a session. After a
restart, `SessionRegistry.get()` builds a new `RetailAgent` with a fresh `RetailDomain`, a
fresh `SessionState` and a message list holding only the system prompt. Four consequences:

1. **The conversation is gone.** The user's next message reaches a model that has never
   seen the previous turns, and the answer contradicts what was already said.
2. **The business state is gone.** A cancellation executed before the restart is absent
   from the new in-memory database, so the agent reports the order as still pending and
   would cancel it a second time.
3. **A pending confirmation is forgotten.** The user is asked to confirm, the process
   restarts, they reply "yes", and the write silently never happens - or worse, the guard
   sees no pending call and the model reissues it.
4. **The session limits reset.** Steps, tool calls and the session deadline all return to
   zero, so restarting is a way around the stop conditions.

M2 solves those four and nothing else.

## 1. Change card

- **Stage and step**: M2
- **Type**: feature
- **The specific problem**: application state is persisted but never replayed, so a
  restart loses the conversation, the business state, the pending confirmation and the
  session limits
- **The single theme of this change**: rebuild a session from the database after a
  restart, without re-executing any write, and stop two processes from driving one
  session at the same time
- **Control variables this change does not touch**: the model under test, the simulated
  user, the judge, the task sets, the upstream commit, the price snapshot, and the M1
  schema's existing semantics. Not one byte of the evaluation stack changes, or the
  comparison against `baseline-v3` means nothing.
- **Explicitly out of scope**:
  - The tau2-bench simulated order data still stays out of PostgreSQL. Recovery
    reconstructs the domain from the `operations` log rather than storing the benchmark's
    business data, for the same reason M1 gave: storing it would rewrite the official
    evaluation expectations.
  - No authentication (M3), no human handover (M4), no multi-node deployment. The claim
    mechanism is built and tested here, but only one process runs in this stage.
- **Expected benefit**: a restart is invisible to the user; a confirmed write executes
  exactly once across a restart; the stop conditions cannot be reset by restarting
- **Possible cost**: one database read per session miss, plus the replay time; a claim
  row read and renewed per turn on the normal path

### 1.1 Primary metric, protective metrics, acceptance thresholds

Fixed before running, per `MODEL_EXPERIMENTS.md`.

| Metric | Threshold | If not met |
|---|---|---|
| Recovery correctness | After a restart, the resumed session's `db_hash` equals the value before the restart | FAIL |
| No re-execution | Recovery adds no `operations` row and no `audit_events` row | FAIL |
| Confirmation survives | A confirmation pending before the restart is still pending after it, and an affirmative reply executes the write exactly once | FAIL |
| Limit continuity | Steps and tool calls resume from their persisted values rather than from zero | FAIL |
| Single owner | With two processes claiming one session, exactly one proceeds and the other is refused | FAIL |
| Normal-path overhead | Recovery runs only on a session miss; the per-turn claim cost is measured and reported | Explain the cause |
| Degradation | With `RSA_DATABASE_URL` unset, behaviour is M0's in-memory sessions | FAIL |
| Evaluation score | No regression against `baseline-v3` | Explain the cause |

- **Baseline**: `baseline-v3` (this project's guarded agent, 102/114, 0 unconfirmed
  writes). The evaluation protocol version is unchanged.
- **Task set**: not applicable to the recovery capability itself, which is verified
  against PostgreSQL 17 rather than through the benchmark. The benchmark's role here is
  only to show the evaluation stack is untouched.

### 1.2 Two design decisions, stated before the work

**Recovery replays the `operations` log rather than snapshotting the domain database.**
The `operations` table is already the authoritative record of every write that succeeded,
M1 guarantees a failed write leaves no row, and the retail tools are deterministic:
`third_party/tau2-bench/src/tau2/domains/retail/tools.py` contains no use of `random`,
`uuid` or a clock, so applying the same arguments in the same order to a fresh copy of
the same initial data yields the same database. `get_db_hash()` is a content hash of the
whole database model, so the equality can be checked rather than assumed.

Replay applies writes directly to the domain and **must bypass `execute_once`**, or
recovery itself would be recorded as new work.

**LangGraph's Postgres checkpointer is not used.** The graph's state here holds only
`status` and `stop_reason`; the conversation lives in `RetailAgent._messages` and the
authorization record lives in the M1 tables. Adopting the checkpointer would either
persist a nearly empty state or require moving the message history into graph state,
which would create a second record of the same conversation that can disagree with the
`messages` table. One authoritative record is worth more here than reusing a library
component. This decision is recorded so that a later reader knows it was considered.

## 2. Implementation

| File | Responsibility |
|---|---|
| `src/retail_support_agent/db.py` | `session_claims` table; `steps` and `tool_calls` on `sessions` |
| `migrations/versions/0002_recovery.py` | The migration for both, with a working `downgrade` |
| `src/retail_support_agent/store.py` | `claim_session` / `release_session` / `claim_owner`, `progress` / `save_progress`, `confirmed_fingerprints`, `last_confirmation_id` |
| `src/retail_support_agent/agent.py` | `RetailAgent.recover()` and `db_hash()`; counters saved after each turn |
| `src/retail_support_agent/api.py` | Claim per turn, rebuild on a session miss, HTTP 409 when contested, release on reset |
| `tests/test_recovery.py` | 19 tests, one group per acceptance threshold |
| `tests/conftest.py` | Pin `TAU2_DATA_DIR` before any test module imports tau2 |

Recovery restores four things, in order: the business state by replaying `operations`
onto the fresh domain; the conversation from `messages`; the authorizations (confirmed
fingerprints, the last confirmation id, any pending confirmation); and the stop
conditions (counters plus the session's age).

### 2.1 The lease is one statement

```sql
INSERT INTO session_claims (session_id, owner, expires_at) VALUES (...)
ON CONFLICT (session_id) DO UPDATE SET owner = ..., expires_at = ...
WHERE session_claims.owner = :owner OR session_claims.expires_at < now()
RETURNING session_id
```

A read-then-write would let two processes both see a free session. `RETURNING` reports
whether the conflict clause fired: an `ON CONFLICT DO UPDATE` whose `WHERE` fails updates
nothing and returns no row, which is exactly "someone else holds it".

The expiry is computed by the database (`now() + interval`), not by the application, so
two processes with different clocks cannot disagree about when a lease ends. The TTL is
60 seconds: longer than a turn, well under the 180-second session limit, so a process
that crashes mid-turn does not block a session for its whole life.

## 3. Results

Verified against PostgreSQL 17 (`compose.yaml`, loopback only) with
`RSA_TEST_DATABASE_URL` set. All 19 recovery tests executed rather than skipped, and the
full suite of 356 tests passed.

### 3.1 The acceptance thresholds

| Metric | Threshold | Measured | Result |
|---|---|---|---|
| Recovery correctness | Resumed `db_hash` equals the pre-restart value | equal on a fresh domain replaying one cancellation | pass |
| No re-execution | No new `operations` or `audit_events` row | both counts unchanged after replay | pass |
| Confirmation survives | Still pending, and the write runs exactly once | pending restored; the same key replayed, 1 operation row | pass |
| Limit continuity | Counters resume | steps 18 / tool_calls 14 restored; a session at the step limit stays stopped | pass |
| Single owner | Exactly one process proceeds | second owner refused, HTTP 409 at the API | pass |
| Normal-path overhead | Measured and reported | claim 4.5 ms median, 7.2 ms p95 | pass |
| Degradation | M0 in-memory behaviour without a database | `recover()` is a no-op, `recovered=False` | pass |
| Evaluation score | No regression against `baseline-v3` | not applicable; see 3.5 | see 3.5 |

### 3.2 Replay correctness rests on a checked property, not an assumption

The claim that replay reproduces the database depends on the retail tools being
deterministic. That was checked rather than assumed:
`third_party/tau2-bench/src/tau2/domains/retail/tools.py` contains no occurrence of
`random`, `uuid`, `datetime.now` or `time()`. The test then verifies the consequence
directly - a fresh domain replaying one cancellation reaches the same `db_hash` as the
original - because the absence of a keyword is weaker evidence than an equal hash.

A second test pins that order matters: two address changes applied in opposite orders
produce different hashes. Writes are not commutative, so replaying them in an arbitrary
order could produce a different database while reporting success. `operations` is ordered
by primary key, which is insertion order.

### 3.3 A failed write is not replayed

M1 guarantees a rolled-back write leaves no `operations` row, so there is nothing to
replay. This has its own test rather than being left implicit: replaying a failed write
would apply a change the user never saw succeed, and the audit trail would then disagree
with the database.

### 3.4 What recovery costs

| Operation | Measured |
|---|---|
| `claim_session` (every turn) | median 4.5 ms, p95 7.2 ms, max 9.5 ms |
| `save_progress` (every turn) | median 4.4 ms, p95 6.9 ms |
| Recovery reads (6 queries) | 17-25 ms, flat from 1 to 50 operations |
| One replayed write | median 0.011 ms |
| `RetailDomain()` construction | 43 ms (3.7 s for the first one in a process, which is `import tau2`) |

Two things follow. The normal path pays about 9 ms per turn for the claim and the
counters, against a model call measured in seconds: real, small, and now measured rather
than assumed. And recovery is dominated by fixed costs, six queries and building the
domain, not by the number of writes to replay, because a replayed write is 0.011 ms. A
session with 50 writes recovers in about the same time as one with a single write.

The domain construction is not new: M0 already built one on every session miss. What M2
adds on a miss is the reads plus the replay.

### 3.5 Not verified

- **No regression in evaluation scores.** The evaluation loop runs without
  `RSA_DATABASE_URL`, so neither the claim nor the recovery path participates and
  `baseline-v3` is unaffected. That is why the threshold is marked not applicable rather
  than passed: no run was made with a database attached, and none is claimed.
- **Multi-node operation.** The lease is built and tested, but only one process runs in
  this stage. What is verified is the mechanism, not a deployment.
- **The tool transcript is not restored.** Only the visible turns are journaled, so after
  a restart the model resumes with the conversation the user saw, not the intermediate
  tool messages. Tool results are re-derivable from `operations`, and storing a second
  copy that could disagree with it was judged worse than the loss. A model that needs a
  read result again will call the tool again, which has no side effect.
- **A crash between the domain write and the commit.** `execute_once` applies the domain
  change inside the transaction, so a crash after `apply()` but before the commit loses
  the operation row while the in-memory domain has already changed. That window closes
  itself: the process dies, the in-memory domain dies with it, and recovery replays only
  committed operations. It would matter if the domain were durable, which is exactly why
  it is not.

### 3.6 What went wrong along the way

The first run of the two real-domain tests failed with a missing `db.json` under
`.venv/Lib/data/`. The cause is an ordering hazard that had been latent in the test suite:
upstream resolves its data directory when `tau2` is imported, so whichever test imports it
first fixes that directory for the whole process, and files that only call
`pytest.importorskip("tau2")` pin the packaged path, which holds no data. Running
`tests/test_recovery.py` alone passed; running the full suite failed.

`tests/conftest.py` now sets `TAU2_DATA_DIR` to the verified checkout before any test
module is imported, and only when it is unset, so an explicit value still wins. This is a
real defect fixed rather than a workaround: any future test needing the real domain would
have hit the same thing, and whether it failed would have depended on file order.

## 4. Conclusion

**PASS.** Seven of the eight thresholds are met and measured; the eighth, no regression in
evaluation scores, does not apply because the evaluation loop does not use the database,
which is stated rather than reported as a pass.

What this step adds, in one line: a restart no longer loses the conversation, the business
state, the user's authorization or the session limits, and no write runs twice as a result
of recovering.

What it leaves open is M2's own boundary rather than a defect: the lease is verified as a
mechanism, not in a deployment with more than one process. That belongs with M6, where
more than one instance actually runs.
