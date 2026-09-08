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

## 2. Implementation and verification

To be filled in after the work, per `docs/CHANGE_REPORT_TEMPLATE.md`.

## 3. Comparison

To be filled in after the work.

## 4. Failure analysis and conclusion

To be filled in after the work.
