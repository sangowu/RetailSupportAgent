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

## 6. What this release does not establish

- **The rest of the interface.** Two passes walked one path in a browser: sign in,
  refusal, audit panel, plus which sections each account is offered. Assignment, working
  a case, closing it, deciding an escalation, approval expiry and the streaming states
  remain covered at the API and database levels only. Section 5 says why the second pass
  stopped where it did.
- **The benchmark under an enforcing scope.** The tau2 adapter still runs with a
  permissive scope so that `baseline-v2` and `baseline-v3` stay comparable, and a test
  asserts it. What an enforcing scope would score is unmeasured, and the number would
  describe a variant rather than this application's token-derived identity control.
- **Concurrency and load.** Unchanged and unmeasured.
- **Rollback across a schema change.** Not engaged by this release and still unvalidated.

## 7. Rollback

`m7.3` remains published and predates no schema this release introduced, so rollback is
the documented parameter change and a restart:

```
terraform -chdir=deploy/terraform/demo apply -var="image_tag=m7.3"
uv run python scripts/demo.py restart
```

That returns the deployment to a version with no access scope, no refusal audit trail and
no audit page. It is a rollback of the application, not of a security control that exists
independently of it.
