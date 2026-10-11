# M11: the remaining order writes over MCP, change record

Status: **deployed; the AWS acceptance was not run.** Image `m11.1` runs on the demo
instance (section 2.5). The plan is [M11_MCP_WRITES.md](M11_MCP_WRITES.md). Its AWS rows
were not run, by the owner's decision on 2026-10-11, so the stage is tested locally and in
CI, and deployed, but not accepted on AWS.

## 1. Change card

Recorded on 2026-10-11 against `main` at `8fbc031`.

- Stage and step: M11, steps 1 to 4 of the plan, on branch `feature/mcp-writes`.
- Type: feature.
- Problem: over MCP a client could cancel and return, but not exchange or change a pending
  order's items, address or payment method, which the agent can do; and the confirmation
  form listed identifiers a person cannot check.
- Single theme: the four order writes M9 left for a second version, on M9's path, with a
  form that says what they will do.
- Controls not touched: model, prompt, reasoning settings, evaluations and baselines, the
  agent path, `WriteGuard`, `Store`, `AccessScope`, `ProposedCall.describe()` and the
  fingerprint, the deployment files.
- Expected benefit: an MCP client can do every order change the agent can, and the user
  sees products, prices and addresses before agreeing. Cost: none at run time beyond two
  or three reads per form.
- Primary metric: the plan's acceptance table, CI rows here, AWS rows after the release.
- Protective metrics: no new failure in the existing suite; the M9 acceptance table and
  the access-case replay unchanged in both protocols; the three baselines verify.
- Reference: the suite on `8fbc031`. No model score is involved, so the MVP comparison
  does not apply.

## 2. Implementation and verification

### 2.1 Commits

| Commit | What it does |
|---|---|
| `ea2ae4b` | Step 1: `get_item_details`, a catalog read |
| `1694af5` | Step 2: the four writes, each with a resolver, a call builder and upstream's parameters; one `_write` helper shared with M9's two |
| `c552821` | Step 3: `form_summary`, and `propose` putting its summary above `describe()` |

### 2.2 Files

| File | Responsibility |
|---|---|
| `src/retail_support_agent/mcp_server.py` | The five tools, their resolvers and call builders; the summary in `propose` |
| `src/retail_support_agent/form_summary.py` | New: the summary for item changes, address and payment changes |
| `tests/test_mcp_order_changes.py` | New: each write in both protocols; the slice, a changed argument, an upstream refusal |
| `tests/test_form_summary.py` | New: the summary on the pinned data |
| `tests/test_mcp_sessions.py`, `tests/test_mcp_server.py` | `get_item_details` read without a slice; a test renamed |
| `reports/mcp_acceptance/2026-10-10.json` | The M9 acceptance report, regenerated on this code |
| `README.md` | Tool table, contract, the form |

### 2.3 Checks before starting

1. **Upstream's descriptions.** Read from the pinned version. The MCP descriptions keep
   upstream's rules (items changed once, a delivered order returned or exchanged once)
   and drop the instruction to ask "yes/no" in chat, which the form replaces.
2. **A failed upstream write.** Upstream refuses after the form, inside the call: a wrong
   status, unknown or mismatched items, a payment method not on the order's user, the
   current payment method again, an insufficient gift card. The domain returns
   `Tool call failed: <reason>`, and `Store.execute_once` records it as the result, so the
   client sees `executed` with that text, as for M9's two writes. Kept, tested
   (`test_an_upstream_refusal_after_the_form_is_reported_and_runs_nothing`) and
   documented in the README.
3. **Data.** `ava` has one payment method, so the payment change is tested on
   `sofia_li_9219` (two credit cards); item changes and exchanges on `raj_sanchez_2970`.

### 2.4 Verification

**Tests.** Before: ruff, format and mypy pass; pytest 1164 passed, 0 failed, 0 skipped.
After: ruff, format and mypy (106 files) pass; pytest 1217 passed, 0 failed, 0 skipped,
in 168 s. Windows, PostgreSQL 17 in Docker.

| New tests | Count |
|---|---|
| Each of the four writes × both protocols: accept then repeat, decline, another customer's order, no form support, the form's text | 40 |
| The guard as for M9 writes: the status slice, a changed argument, an upstream refusal | 3 |
| The summary on the pinned data | 9 |
| `get_item_details` without a slice | 1 |

**The M9 acceptance script** (`scripts/run_mcp_acceptance.py`) on this code: all passed in
both protocols. Compared with the committed report, the only difference is the
`pending_confirmations_after` field M10 added; the access-case replay is identical
(22 cases, 17 applicable, 17 blocked, 0 forms sent). The ten `Exception in callback`
lines in its log appear on `main` too: Windows' asyncio closing connections.

**An example form**, Raj exchanging headphones:

```
Exchange items in order #W7736708:
- Headphones: type on-ear → over-ear, color red → black; $377.97 → $346.97.
Price difference: $31.00 refunded to credit_card_3362387.
Prices are as of now; the amount is worked out again when it runs.

Run exchange_delivered_order_items with these arguments:
  - item_ids: ["3104857380"]
  - new_item_ids: ["7493556126"]
  - order_id: "#W7736708"
  - payment_method_id: "credit_card_3362387"
```

### 2.5 Release on AWS; the acceptance not run

**Release.** Image `m11.1`, built from `main` at `89517e5` (#79), 226 MB, digest
`sha256:fa72c53f1c592495eb7f36efa992513d7bbe218b40af8f2436ac36ebf38cdd33`, pushed to the
existing ECR repository. Listed inside the image before the push: 12 tools, five reads and
six writes. The demo plan was `0 to add, 1 to change, 0 to destroy`, the change being
`aws_ssm_parameter.image` (`m10.1` to `m11.1`); the instance's refreshed state was the
only other difference shown. The saved plan file was applied. The instance was stopped,
so the release was `demo.py start` (ready in 96 s). `demo.py status`: running and
configured both `m11.1`; `/health` reports `m11.1`.

**Archive.** `archives/rsa-20261011T005501Z.sql.gz`, taken before any write. On the
instance, `operations` had 0 rows and `confirmations` none. No write was made afterwards,
so no restore was needed; the archive is kept.

**Acceptance: not run.** The owner decided on 2026-10-11 not to run the plan's AWS rows.
None of them has a result:

| Plan row | Status |
|---|---|
| Tools listed with a real token: 12 | not run; 12 listed inside the image |
| Claude Code: address change on `#W4135875`, declined then accepted | not run |
| Claude Code: exchange on `#W4817420` | not run |
| `get_item_details` for an exchange's new item | not run |
| The existing demo: sign-in, `/health` reports `m11.1` | `/health` reports `m11.1`; sign-in not run |
| Rollback to `m10.1`: 7 tools listed | not run; `m10.1` remains in ECR |

What stands in their place is the local and CI evidence of section 2.4: each write in both
protocols against a real PostgreSQL and the pinned data, and the M9 acceptance table and
access-case replay unchanged. What it does not show is a real client's form over the
deployed stack, which is what the Claude Code rows were for. They need one `ava` token and
about twenty minutes, and can be run later against `m11.1` with the plan's table as is.

## 3. Comparison

| Metric | Before | After | Conclusion |
|---|---|---|---|
| Tests passing | 1164 | 1217 | 53 added, none failing |
| M9 acceptance table, both protocols | 10/10 | 10/10 | unchanged |
| Access cases replayed through MCP | 17/17 blocked, 0 forms | 17/17 blocked, 0 forms | unchanged |
| Tools listed | 7 | 12 | as planned; in the image, not over AWS |
| Task success, safety violations, P95, cost | not applicable | not applicable | no model is called by this change |

## 4. Failure analysis and conclusion

- **A refused write reads as executed.** A call upstream refuses after the form returns
  `executed` with `Tool call failed: ...` as its result, and a repeat replays that answer.
  This is M9's behaviour, now reachable by more rules. The summary makes the common
  causes visible before the user agrees (an item not in the order, lists that do not pair
  up), but not a gift card balance, which it does not show. Changing it would change what
  `Store` records, which is outside this stage; it is listed for a later one.
- **The summary reads at question time.** Prices could in principle change between the
  form and the call; upstream computes the charge when it runs, and the summary says so.
  The demo data does not change prices.
- **Conclusion: PASS for implementation; INCONCLUSIVE for the stage.** Released as
  `m11.1`; the AWS acceptance was not run (section 2.5).

## 5. Differences from the plan

| Plan | Built | Why |
|---|---|---|
| Six summary cases in step 3 | Nine: also an unreadable order, lists of different lengths, and no summary for cancel and return | Each is a branch of the function |
| - | A `_write` helper used by all six writes | The four new bodies would otherwise repeat M9's call verbatim |
| - | The M9 acceptance report regenerated and committed | It predated M10's added field; the run is this stage's protective check |
| Step 6, the acceptance on AWS | Not run | The owner's decision, 2026-10-11 |
