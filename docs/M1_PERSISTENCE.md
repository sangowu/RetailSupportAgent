# M1: application state in PostgreSQL (migrations, transactions, idempotency)

## 0. Why this step exists

M0 kept sessions in process memory: `SessionRegistry` was a `dict[str, RetailAgent]`,
lost on restart. Three consequences:

1. **A retry executed the write twice.** A client timeout and resend made the agent
   cancel the order again. The M0.6 guard guarantees that a write is confirmed first,
   but not that a confirmed write runs only once.
2. **Confirmations had nowhere to live.** An operation the user confirmed on turn 3
   could not be verified after a restart.
3. **There was no audit.** Who authorized which write action and when existed only in
   log text.

M1 solves those three and nothing else.

## 1. Change card

- **Stage**: M1
- **Type**: feature
- **Problem**: application state is not persisted, retries repeat writes, and
  confirmation and authorization have no audit record
- **Single theme**: application state in a database, with migrations, transactions and
  idempotency
- **Control variables not touched**: the model under test, the simulated user, the
  judge, the task sets, the upstream commit, the price snapshot. Not one byte of the
  evaluation stack changes, or the comparison against `baseline-v3` means nothing.
- **Explicitly out of scope**:
  - The τ²-bench simulated order data stays in upstream's in-memory environment. Moving
    it into PostgreSQL would amount to rewriting the official evaluation expectations,
    making scores incomparable immediately.
  - No authentication (M3), no LangGraph checkpoints (M2), no tenancy or billing.
- **Expected benefit**: retries and concurrency do not repeat a modification; a failure
  rolls back as a whole; every write has a traceable authorization chain
- **Possible cost**: one more database round trip per turn; one more component to deploy

## 2. What is persisted (application state only)

| Table | Contents | Key constraint |
|---|---|---|
| `sessions` | The session and its simulated identity | - |
| `messages` | Conversation turns | `(session_id, seq)` unique, so a replay cannot insert a second row |
| `confirmations` | Pending and resolved write actions with their argument fingerprint | At most one pending row per session |
| `operations` | Write actions actually executed | `idempotency_key` unique; idempotency rests on it, not on application logic |
| `audit_events` | Authorization basis and outcome | Foreign key to `operations` |

Invariants belong to the database, not to application code: unique constraints, foreign
keys, conditional updates. An application-level "check whether it exists" misses under
concurrency; a unique constraint does not.

## 3. Definition of idempotency

Key = `sha256(session_id | tool | canonical arguments | confirmation_id)`.

- The same key arriving a second time does not execute; the first result is returned.
- Changed arguments change the key and require a new confirmation, using the same
  fingerprint definition as the M0.6 guard.
- Uniqueness is enforced by the unique index on `operations.idempotency_key`. An insert
  conflict means "already executed", so no SELECT-then-INSERT is needed.

## 4. Acceptance thresholds (fixed before running)

| Metric | Threshold | If not met |
|---|---|---|
| Repeated execution | One key repeated N times executes the write once | FAIL |
| Failure rollback | A mid-transaction failure leaves neither the operation row nor the business effect | FAIL |
| Concurrency | Concurrent submissions of one key execute once; the others return that result | FAIL |
| Evaluation score | No regression against `baseline-v3` (McNemar not significant is enough) | Explain the cause |
| Migrations | `alembic upgrade head` and `downgrade base` both run | FAIL |

## 5. Results

Verified against PostgreSQL 17 (`compose.yaml`, loopback only), with
`RSA_TEST_DATABASE_URL` pointing at it. SQLite is not a substitute: partial unique
indexes, unique conflicts and transaction rollback are dialect-specific, and a green run
on another database would show nothing.

### 5.1 The four acceptance thresholds

| Metric | Threshold | Measured | Result |
|---|---|---|---|
| Repeated execution | One key repeated 5 times executes once | executed once, one `operations` row | pass |
| Failure rollback | Neither the operation record nor the business effect remains | `operations` and `audit_events` both empty | pass |
| Concurrency | 8 concurrent submissions of one key execute once | executed once; the other 7 returned that result | pass |
| Migrations | `upgrade head` and `downgrade base` both run | ran in a separate schema | pass |

All 14 persistence tests executed rather than skipped, and the full suite of 340 tests
passed.

### 5.2 Concurrency is handled by the index, not the application check

"Executed once" on its own does not say what prevented the second execution: a
pre-check would produce the same number when the calls happen to be serialized. So it
was verified separately: `apply()` was slowed to 0.4 seconds, guaranteeing all 8 threads
pass the pre-check before the first commit.

All 7 replaying threads returned after 0.42 seconds, having been blocked by the unique
index until the first transaction committed. Had the pre-check stopped them, they would
have returned in 0.00 seconds.

The pre-check is only a fast path that avoids a conflict. Correctness comes from the
unique index.

### 5.3 A failure does not burn the key

This has its own test: after a transient fault, the same key must still be usable.

If a failure claimed the key, one network blip would make the operation permanently
unexecutable while reporting "already done", which is the hardest failure to diagnose.
With `apply()` raising, the whole transaction rolls back, the operation row disappears
with it, and the key is free.

### 5.4 A second authorization is not a retry

The idempotency key includes `confirmation_id`. Confirming the same arguments again is a
new authorization and should run again: the user may genuinely want to return another
identical item. Without it, the second legitimate request would be swallowed as a retry
while the user believed it had succeeded.

### 5.5 It must run without a database

With `RSA_DATABASE_URL` unset, `Store` is None, every persistence call is a no-op, and
behaviour returns to M0's in-memory sessions.

This is not a courtesy fallback: the evaluation loop has no PostgreSQL, so a hard
dependency would stop τ²-bench from running at all and make the `baseline-v3` comparison
impossible.

### 5.6 What went wrong along the way

`alembic.ini` contained non-ASCII comments. On Windows, configparser reads that file with
the machine's locale encoding (GBK), and the migration test failed with
`UnicodeDecodeError`. The file is now ASCII-only, with the explanation in
`migrations/env.py`.

### 5.7 Not verified

- **No regression in evaluation scores.** Evaluation runs without a database configured,
  so the persistence path does not participate and `baseline-v3` is unaffected. Proving
  that scores are unchanged with a database attached would require another full run,
  which was not done and is not claimed.
- **Resuming a conversation across processes.** After a restart the agent instance and
  the domain environment are rebuilt, and although the message history is persisted it is
  not replayed. That is M2's subject, now delivered in [M2](M2_RECOVERY.md).
