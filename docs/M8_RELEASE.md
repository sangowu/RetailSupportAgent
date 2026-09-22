# M8 release: putting the access controls where a person can reach them

## 0. Why this record exists

Everything M8 added - the per-session access scope, the status slice, output attribution,
the refusal audit trail and the page that reads it - had been verified in unit tests, in
the frozen access case set and in the adversarial set. None of it had ever run in the
deployed environment. The demo answered on image `m7.3`, published 2026-09-10, which
predates all of it.

This release moved the deployed environment from `m7.3` to `m8`, and then to `m8.1`
after a browser acceptance found a defect that no test in this repository could have
found. Both halves are recorded here, because the second is the reason the first was
worth doing at all.

## 1. What was published

| Tag | Commit | Digest | Pushed |
|---|---|---|---|
| `m8` | `0aa338b` (#48) | `sha256:6454ff11525c75d5adc0b5a2578b50dd7a895888421909fcfcf43ab3567e5d0a` | 2026-09-21T10:29:12Z |
| `m8.1` | `503a840` (#49) | `sha256:c75ae33c8d39f0d718d97562f17d134783257251335849e9208cdf2d5bfb64ed` | 2026-09-21T11:31:23Z |
| `m8.2` | `97a68d8` (#52, #53, #54, #55, #56) | `sha256:5edefcde9a7c8300591aaac9010d53998ee9de165160840ec5e2cf1b2f74e44e` | 2026-09-21T16:56:26Z |
| `m8.3` | `9915644` (#57, #58) | `sha256:66a87465797f0ccdd41bfc0f3259191a497647c64ac8a3ea4ffbee54821c6f57` | 2026-09-21T21:27:43Z |
| `m8.4` | `e1915aa` (#59) | `sha256:7303763d87d8fe24224e7f0fa47fac2cb48357cdeeb080349c4695c5f2847999` | 2026-09-21T22:55:20Z |
| `m8.5` | `e6eef14` (#61) | `sha256:e905c31134c16a8d46ce4a9526a6ea997fc72966bb6c4a187288fa9bb9c014e7` | 2026-09-21T23:43:00Z |
| `m8.6` | `83e0d61` (#63) | `sha256:ab8c22111acaf9a71217bf0eaa737b1dfe65adfdcfd6389849165ec8c3cb9c06` | 2026-09-22T21:12:40Z |

The repository's ECR tags are immutable, so each digest is the only image that tag has
ever named. `m7.3` and `m6.8.1` are retained for rollback.

## 2. Checks that ran before publishing

Against the revision each image was built from:

| Check | Result |
|---|---|
| `ruff check` and `ruff format --check` | clean, 143 files |
| `mypy src tests scripts` | clean, 92 source files |
| `pytest -m "not live_model"` with `RSA_TEST_DATABASE_URL` set | exit 0 |
| `scripts/run_access_eval.py` | 22/22 blocked, `access-v2` |

The database URL matters: without it the eight tests in `tests/test_access_audit.py`
skip, and those are the ones that exercise the write path this release exists to
deliver. A release verified with them skipped would have been verified around the change.

The image itself was then checked rather than assumed. Running `migrate` from the built
`m8` image against a local PostgreSQL reached head without applying anything, which is
the expected result: M8 added no migration, so this release does not cross a schema
boundary and the rollback constraint recorded in M6.8 is not engaged. Starting the image
with `api` answered on `/health` reporting its own tag, and its OpenAPI document listed
`/session/{session_id}/audit`, so the route was confirmed present in the artifact rather
than only in the source tree.

## 3. The release itself

Both releases produced the plan M6.8 was built to produce:

```
Plan: 0 to add, 1 to change, 0 to destroy.

  # aws_ssm_parameter.image will be updated in-place
  ~ value = (sensitive value)
```

Instance `i-0e4b76e870a950271` and its root volume were carried through both changes.
The user-data discrepancy resolved after M7 did not reappear; the plan proposed no
replacement either time.

One operational note worth recording: the demo layer's `terraform.tfvars` is untracked by
design, and this machine no longer had it. It was reconstructed from the running
environment rather than guessed - the bucket from the base layer's output, the ACME
address and domain from the stopped instance's decoded user data, and the allowed source
addresses from the demo security group - and the plan above is what confirmed the
reconstruction was faithful. A variable file that is deliberately not in version control
is a file that can go missing, and recovering it from the environment is the procedure.

The `m8.1` restart wrote a database archive at 2026-09-21T11:31:37Z. The previous archive
was from 2026-09-10, which is what a demo that has not been released to in eleven days
looks like.

## 4. Verification after each release

```
running:    569260897196.dkr.ecr.eu-west-1.amazonaws.com/rsa-app:m8.1
configured: 569260897196.dkr.ecr.eu-west-1.amazonaws.com/rsa-app:m8.1
/health     {"status":"ok","auth":"required","image":"...:m8.1"}
```

`demo.py status` reports the configured and the running version separately, and they
differ exactly while a restart is in flight or has failed. They matched after both
releases.

## 5. What the browser acceptance found

The acceptance was: sign in as `sam`, a supervisor, ask for an order belonging to another
customer, and read the refusal in the audit panel.

The refusal behaved correctly. `#W2611340` belongs to `james_li_5688`; the session's
retail identity is `ava_moore_2033`; the agent declined. The audit row, read from the
deployed page, was:

```
2026-09-21T11:39:42.871342+00:00 | access_denied | get_order_details | self |
Order #W2611340 does not exist or its owner cannot be confirmed.
```

Every field is what the design intended. The kind is the string the database stores
rather than a prettified label, so the screen and a query use one vocabulary. The action
names the tool rather than reporting a refusal in general. The authority is `self`,
because this session was acting on its own identity and not under a ticket. The reason is
worded identically for an order that belongs to someone else and an order that does not
exist, so the difference between the two cannot be read off the error.

**But on `m8` the row could not be reached.** The panel required a session to be named
before it would fetch anything, and a session id is state the page holds and never
displays. A supervisor who had just been refused something had no way to learn the id of
the conversation they were sitting in. The permission was held, the record was written,
the route answered - and there was still nowhere to use it, which is the exact dead end
the panel had been built to remove.

#49 fixed it: a blank box means this session, naming another still works, and the note
names whichever was read so the id becomes something a person can see and copy. `m8.1`
carries that fix, and the row above was read with the box left empty.

### Why no test could have caught it

Every unit test, every case in `access-v2` and every attack in `attacks-v3` supplies a
session id, because at those layers a session id always exists. The defect lived in the
gap between a correct backend and a person with no route to it, and that gap is only
visible from a browser. This is the second time in M8 that a check was found to be
measuring the wrong half - after `ran` versus `held` and the unplanted injection payloads
- and like those it was fixed structurally rather than patched at the call site.

It is also the direct answer to a limitation this repository had been carrying since M6:
that no complete browser acceptance had been performed. One narrow browser acceptance
found a defect on its first attempt.

### A second pass, on the fixed build

The first pass established that the panel was unreachable. A second pass, after `m8.1`
was released, walked the same account through the page again and checked the fix from
both sides rather than only the one that had failed.

| Checked | Result |
|---|---|
| Identity shown after sign-in | `Acting as ava_moore_2033 · roles: supervisor` - the account and the retail identity are separate, and the page says which is which |
| Sections offered | Cases, Approvals, Audit trail; **no "Work this case"**, which a supervisor does not hold `ticket:work` for |
| Close offered | Yes, and correctly: `/tickets/{id}/close` admits the assignee or any holder of `ticket:assign`, and a supervisor holds it |
| Empty case and approval lists | Rendered as empty tables with their headers, not as blank panels |
| Cross-customer read | Refused, worded so that a foreign order and a non-existent one cannot be told apart |
| Audit panel, box left blank | `1 record(s) for d138ff33…`, the row correct in all five fields |
| Audit panel, box naming another session | `No records for 0000…dead`, zero rows - it does **not** silently fall back to the current session |

The last two are the two sides of the fix. Blank has to mean this session, and a value
that was typed has to be honoured; a panel that quietly substituted the current session
for an id that returned nothing would be worse than the dead end it replaced, because it
would answer a question nobody asked and look like an answer.

The row read from the page on this pass:

```
2026-09-21T15:27:26.825862+00:00 | access_denied | get_order_details | self |
Order #W2611340 does not exist or its owner cannot be confirmed.
```

### What the second pass could not reach

Assignment, working a case, closing it and deciding an escalation all need a case to
exist, and a case is only created when a customer's conversation is handed off. That
needs a second account, and the browser used for the pass had a stored credential for
one account only. The four workflows remain covered at the API and database levels and
unverified in a browser; the sign-in refusal for the account that could not be used was
itself correct, giving the same message for a wrong password as for an unknown username.

## 5a. The full acceptance, and the eight defects it found

Section 5 walked one path. A later pass walked the rest - sign in as each role, read the
case list, hand a case off, assign it, work it, propose a write, escalate it, decide it,
close it - and found a defect at almost every step. None of them was reachable from the
test suite, and one of them the suite actively asserted.

| # | What a person saw | What was wrong |
|---|---|---|
| #52 | A closed case could not be audited | The case row did not carry its session id, so the only ids a supervisor could reach were the one they were in and any with an approval still pending |
| #53 | A customer sat in front of a disabled box, told an answer was on its way | `handed_off` and `waiting_for_human` closed the input. Nothing pushes to this page, so the only state that could have discovered the resolution was the one the closed box prevented |
| #55 | `order_id: [withheld: not read in this turn]` on an escalation | The guard's own quoted call was passing through output attribution, which exists for what the *model* says |
| #56 | `Case 3 is with 3275a474-60b1-70e8-...` | The page held the name and showed the subject |
| #58 | The same, still, for an agent | #56 resolved the subject against a roster gated on `ticket:assign`. A supervisor holds it; an agent does not, and an agent's whole view is their own cases |
| #54 | A session died mid-escalation | A supervisor's deliberation was charged to a budget meant to bound a runaway model |
| #57 | The same, still, in production | #54 hooked `send`. The page uses `stream`. Two hand-written routes, one changed |
| #59 | A case session was dead on its first message, permanently | The budget was read as the age of the session row, and a case session is reached again by an id derived from the ticket and the agent |

The shape they share: **correct at every layer and unusable by the person who has to act
on it.** The permission was held, the row was written, the route answered.

### Two of them were fixes for the previous fix

#58 repaired #56, and #57 repaired #54. Both first attempts were verified - by unit tests
that passed, and in #56's case by a browser - and both were verified against the half
that was visible from the screen in front of them. The corrections generalise:

- **A fix verified only by unit tests has been verified against the mechanism, not
  against the entry point the product calls.** #54's tests constructed a `SessionState`
  and called its methods; they never went through `stream()`, which is what the page
  uses. #57 added a parity test parametrised over both routes, and its mutation check
  removes the hook from one route and fails only that route's case.
- **A fix verified from one role's screen has been verified for one role.** #56 was
  confirmed as a supervisor, where it worked. The column it repaired belongs to the role
  it did not repair.

### What the suite could not have caught, and what it got wrong

`test_a_state_that_cannot_be_advanced_closes_the_box` asserted the #53 defect. It was
replaced by two tests that state what the page is for: waiting on a person leaves the box
open, and the advice does not promise delivery the page has no mechanism to make.

The `send`/`stream` parity tests compare the reply and the database after one
conversation. The session clock is neither, so the two routes were already inconsistent
while every comparison passed.

### Two measurement errors of my own, recorded because the method matters

**Reading a table through the DOM.** A Gradio dataframe renders a horizontally scrolling
grid whose rows do not map cleanly onto `tbody tr`. Scraping it produced cells
concatenated across rows, from which two defects were reported that did not exist - a
missing open case, and a closed case that had regained an assignee - and several rounds
were spent looking for a cause in `_summarize` and `close_ticket`. A single full-page
screenshot ended it. Both claims were retracted.

**Diagnosing from the previous diagnosis.** The 180s death observed on `m8.2` was
attributed to the `send`/`stream` split. That split was real and #57 fixes it, but it was
almost certainly not the cause of what was on screen: #59's defect kills a case session on
its first message regardless of route. The method that worked was the one used for #59 -
a discriminating test before any code was read. A message to the case session was refused
instantly; **New session**, same account, same minute, same instance, ran tools and
answered. One variable.

### Skipping the four workflows was the wrong call

Before this pass, the four unverified workflows were recommended for skipping, on the
evidence that M8 had not touched the code behind them - checked with
`git diff 3663d2d..HEAD`, so the evidence was real. Walking them anyway found four
defects. **Untouched is not the same as working**, and for a path no browser had ever
walked there was no prior observation for "untouched" to preserve.

## 5b. `m8.4` crosses a schema boundary

The first four M8 images added no migration, so each release was a parameter change and a
restart, and rollback was the same in reverse. `m8.4` adds `0005_session_worked_seconds`.

What was checked before publishing, against a database built by the previous image rather
than a fresh one - a fresh database proves the migration runs, not that it migrates:

| Step | Result |
|---|---|
| Build a database with the `m8.3` image | at `0004_tickets` |
| Insert a session row with counters | `steps=7, tool_calls=4` |
| Upgrade with the `m8.4` image | `0004_tickets -> 0005_session_worked_seconds`; the row intact, the new column `0` |
| Downgrade with the `m8.4` image | column gone, the row and its counters intact |
| Run `m8.3`'s `migrate` against the downgraded database | reaches head without applying anything |
| Upgrade again, start `api` | `/health` answers; `Ticket.assignee_user_id` present in the OpenAPI document |

Existing rows start at `0` rather than being seeded from `created_at`. Seeding would
carry the defect forward into exactly the rows it broke.

### The restart guard did its job

`demo.py restart` refused: `not restarting, work is unfinished: 1 open ticket(s)`. The
acceptance case was still open, and restarting would have dropped work in flight. It was
closed through the page rather than overridden with `--force` - which also walked the one
remaining unverified workflow, correctly: `Case 3 is rejected. The customer's session
runs again.`

## 5c. The one defect a browser could not have found

Every defect in section 5a was found by a person using the interface. This one needed
concurrent requests constructed on purpose, and it was the more serious of them.

`RetailAgent.send` has always documented that calls for one session must be serialized.
`Store.claim_session` enforces that across processes and renews rather than refuses for
the owner that already holds the lease - which is what lets a process resume its own
session, and grants every concurrent request inside it. `/chat` is a sync route, so
FastAPI runs it in the threadpool and those requests are genuinely parallel.

Two concurrent messages to one session, measured before anything was changed:

```
peak concurrent model calls: 2
result: ('first message',  200, 'ok')
result: ('second message', 200, 'ok')
journaled: [('user', 'first message'), ('assistant', 'ok')]
progress: steps=1
```

| Observed | What it means |
|---|---|
| Both callers got `200` | Both were told their message had been handled |
| One user message missing from `messages` | `_journal` reads the next sequence number and then inserts. Two turns computed the same one and the loser's row was swallowed as a duplicate - a swallow written for a *resent* turn, eating a genuinely different message |
| `steps=1` for two turns | Every stop condition undercounts, so concurrency was a way around all four limits - the hole `test_a_restart_cannot_reset_a_reached_limit` closes for restarts |

The silent message loss is the part worth stating plainly: the system told a person their
message had been dealt with and kept no record of it.

#61 holds a per-session lock for the whole turn, taken before the agent is built so a
refused request touches nothing on its way out. Refused rather than queued, with the same
409 a second process gets: a caller cannot act on the difference between the two reasons
a session is busy, and queueing would let one stack turns against a budget that is per
session. The streaming route releases after its last event rather than when the route
returns, including when the client hangs up. The lock is per session, so two sessions
still run at the same time.

The page's wording followed the cause. A 409 used to mean another process and the advice
was to start a new session; the ordinary cause now is a second click before the first
answer arrived, which clears in seconds.

### Why `m8.5` was not walked in a browser

The condition is not reachable from an ordinary page session. Gradio's `gr.State` is per
page-load, so two tabs hold two different sessions and cannot collide. What can collide
is an agent's case session, whose id is derived from the ticket and the agent precisely so
that returning to a case continues it - and reproducing that needs a case created,
assigned and opened in two tabs.

Skipping the browser step here is a different judgement from the one section 5a records as
wrong. Those four workflows had never been exercised by anything, and "the code was not
touched" was being used as evidence that they worked. This condition was exercised
directly, before and after, with the measurement above. What a browser pass would add is
whether the page can produce the condition, and the answer is largely settled by how
Gradio holds session state.

### A test that was permissive on the machine holding the secret

The first CI run of #61 failed where the local run had passed. `Settings()` requires a
model key, `conftest` clears the real one, and `Settings()` reads `.env` - which exists on
a development machine and not in CI. So the tests passed wherever a real credential was
present and failed where none was, which is the wrong way round.

Fixed as every other module does it, with a fake key set in the module, and verified by
running the whole suite with `.env` moved aside rather than by pushing again to see.

## 5d. `m8.5`

No migration: run against a database the `m8.4` image had built, `m8.5`'s `migrate`
reaches head without applying anything. Rollback from `m8.5` to `m8.4` is therefore the
plain parameter change, with no schema step.

| Check | Result |
|---|---|
| `ruff`, `ruff format --check`, `mypy` | clean, 93 source files |
| `pytest` with `RSA_TEST_DATABASE_URL` | 1001 passed |
| `scripts/run_access_eval.py` | 22/22 blocked, `access-v2` |
| `migrate` from the image, on an `m8.4` database | head, nothing applied |
| `api` from the image | `/health` answers; `SessionRegistry.begin_turn` and `end_turn` present in the artifact |
| Deployed | `/health` reports `...:m8.5`, digest matching the local build |

The instance took about six minutes to answer after the restart - the image pull, the
migration check and the stack coming up. `demo.py status` reported `configured: m8.5` and
`running: not reported` throughout, which is the pair of fields behaving as designed while
a restart is in flight.

## 5e. `m8.6`, and a defect found by measuring rather than by using

The ninth defect was found by walking the interface and the tenth by constructing
concurrent requests. This one was found by asking a question no interaction would have
raised: what does one session cost, and what gives that memory back?

Nothing did. Every session held its own copy of the retail domain in a dictionary that
only ever grew, and the demo runs on a t3.small - 2 GB, shared with PostgreSQL, Caddy,
the API and the UI. Measured on this machine, over the process's own baseline:

| sessions | unbounded | capacity 8 |
|---|---|---|
| 20 | 514 MB | 428 MB |
| 40 | 749 MB | 427 MB |
| 60 | 983 MB | 466 MB |
| 80 | 1218 MB | 489 MB |

About 11.7 MB per session after the first. The reach is what makes it more than a
housekeeping note: the page mints a fresh session id on sign-in and on `New session`, so
any signed-in visitor could exhaust the instance by reloading the page - holding no
permission, breaking no rule, and doing nothing an ordinary user would recognise as
abuse. Both arms of that table were measured. Projecting the second from the first would
have been the same mistake section 5a records twice.

The fix is a bounded LRU cache, `RSA_SESSION_CACHE_SIZE`, default 32. Eviction ends
nothing: a session's state is in the database and `recover()` rebuilds it on the next
message, which is the M2 path every restart already exercises. A session in the middle of
a turn is skipped rather than evicted, because dropping an agent under a running turn
would release the lease that says this process is driving it and leave the turn writing
into an object the next message cannot reach; being briefly over capacity is the smaller
problem. The turn lock is removed with the agent, since a lock per caller-supplied
session id, never cleaned up, is the same unbounded structure in miniature.

Two attempts to state the mid-turn rule end to end both passed against a mutation that
evicted running turns - the first because the held turn finished before the pressure
requests arrived, the second because holding it longer than `CLAIM_TTL_SECONDS` let the
lease expire on its own, so nothing was evicted at all. Holding a turn open past its lease
is not a situation the design has, and a test that must construct one is testing the
harness. The rule is asserted against the registry instead: no threads, no HTTP, no clock.
The mutation now fails exactly one test.

| Check | Result |
|---|---|
| `ruff`, `ruff format --check`, `mypy` | clean, 94 source files |
| `pytest` with `RSA_TEST_DATABASE_URL` | 1013 passed |
| `pytest` with `.env` moved aside | 1013 passed, reproducing CI's environment |
| `scripts/run_access_eval.py` | 22/22 blocked, `access-v2` |
| `migrate` from the image, on an `m8.5` database | head, nothing applied |
| `api` from the image | `/health` answers; default ceiling `32` and `_evict_past_capacity` present in the artifact |
| Deployed | `/health` reports `...:m8.6`, digest matching the local build |

The second row exists because of section 5c: the suite was permissive on the machine
holding the secret, and running it the way CI will run it is cheaper than pushing to find
out. No deployment sets `RSA_SESSION_CACHE_SIZE`, so the ceiling in effect is the default.

What is verified in the deployed environment is that the artifact carrying the ceiling is
the one answering. Exercising the ceiling there would need a signed-in token, and the
evidence for the behaviour itself is the artifact check and the registry tests above.
`active_sessions` on `/health` is the standing observation: under ordinary use it must
never exceed 32.

## 6. What this release does not establish

- **Approval expiry and the streaming states.** A later pass walked assignment, working
  a case, escalation and closure in a browser (section 5a), so those are no longer
  browser-unverified. Approval expiry - a decision that runs out of time before it is
  used - and the intermediate streaming states are still covered at the API and database
  levels only.
- **The benchmark under an enforcing scope.** The tau2 adapter still runs with a
  permissive scope so that `baseline-v2` and `baseline-v3` stay comparable, and a test
  asserts it. What an enforcing scope would score is unmeasured, and the number would
  describe a variant rather than this application's token-derived identity control.
- **Load.** Throughput under many sessions at once is still unmeasured. Two questions
  that were hiding inside "load" have left this list: concurrency as a correctness
  question (section 5c) and per-session memory growth (section 5e), each with the defect
  it was hiding and the tests that now hold the rule. What remains is genuinely
  performance - latency and throughput under concurrent traffic, never measured.
- **Rollback across a schema change on the deployed database.** `m8.4` engages it, and
  the sequence was validated locally against a database the previous image had built
  (section 5b). It has not been exercised on the deployed instance, and doing so would
  mean rolling the demo back on purpose.

## 7. Rollback

Every M8 image is still published, and so is `m7.3`. Rolling back from `m8.6` to `m8.5`
is the documented parameter change and a restart, with no schema step between them:

```
terraform -chdir=deploy/terraform/demo apply -var="image_tag=m8.5"
uv run python scripts/demo.py restart
```

**One step comes first when rolling back past `m8.4`**, the only M8 image that added a
migration. The schema has to be moved before the older code sees it:

```
docker run --rm -e RSA_DATABASE_URL="<the deployed database>" \
  --entrypoint alembic 569260897196.dkr.ecr.eu-west-1.amazonaws.com/rsa-app:m8.4 \
  downgrade 0004_tickets
```

Section 5b records that sequence being run in both directions against a database the
previous image had built.

Going all the way back to `m7.3` returns the deployment to a version with no access scope,
no refusal audit trail and no audit page - and, now, none of the eight fixes in section 5a
or the one in 5c. It is a rollback of the application, not of a security control that
exists independently of it.
