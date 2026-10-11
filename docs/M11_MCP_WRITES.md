# M11: the remaining order writes over MCP

> Plan, 2026-10-10. Implemented and deployed on 2026-10-11 as image `m11.1`; the AWS
> acceptance rows were not run. What was built is in the
> [M11 change record](M11_CHANGE_RECORD.md). M9 built the MCP server with two writes
> ([M9 change record](M9_CHANGE_RECORD.md)), and M10 deployed it
> ([M10 change record](M10_CHANGE_RECORD.md)).

## Goal and scope

Offer over MCP the four order writes M9 left for a second version, so that an MCP client
can do what the agent can do with an order: exchange delivered items, and change a pending
order's items, shipping address or payment method. Add the catalog read an exchange needs.
Make the confirmation form say what a write will do, not only which identifiers it takes.

**In scope:**

- Five tools, with upstream τ²-bench's names and parameters:
  `exchange_delivered_order_items`, `modify_pending_order_items`,
  `modify_pending_order_address`, `modify_pending_order_payment` (writes) and
  `get_item_details` (catalog read).
- A summary at the top of the confirmation form, built by the MCP server from the order
  and the catalog: product names, the options that change, prices, the price difference
  and which payment method pays or is refunded.
- One release through M10's path and an acceptance run on AWS.

**Out of scope:**

- No change to what a call is allowed to do. `WriteGuard`, `AccessScope` and `Store` decide
  exactly as in M10. `WRITE_TOOLS` already lists all four writes, so the guard's
  permission, ownership, status-slice and confirmation checks apply to them unchanged.
- `ProposedCall.describe()` and the fingerprint do not change. The API's confirmation
  text, the agent path and every evaluation stay as they are.
- `modify_user_address`, `calculate` and the two account lookups stay unregistered. The
  access case `modify-other-users-address` therefore keeps being refused by the protocol.
- No model is called and no evaluation is re-run: the agent is not touched.
- No new AWS resource.

## What exists

| Part | Today | What M11 does with it |
|---|---|---|
| `domain.WRITE_TOOLS` | All seven upstream writes | Unchanged |
| `WriteGuard._review` | Permission, ownership (`order_id`, `user_id`), status slice, approval, confirmation for any write in `WRITE_TOOLS` | Unchanged |
| Upstream checks | The tools themselves refuse a wrong status, unknown items, items of another product, an unknown payment method or one not on the order's user, and an insufficient gift card balance | Unchanged; they run after the confirmation, as for the two M9 writes |
| `mcp_server` | `READ_TOOLS` (4), `WRITE_TOOLS_EXPOSED` (2), the handoff; one resolver and one call builder per write | Five more tools, four resolvers, four call builders, the form summary |
| `RetailMcp.propose` | Returns `proposed.describe()` as the form's message | Returns the summary followed by `describe()` |
| M9 acceptance table | Ten scenarios, both protocols; the access cases replayed through MCP | Unchanged; the replay counts the same, since no access case uses the new tools |

## Design

### The tools

Each write follows the M9 pattern exactly: a resolver that asks
(`Resolve(ask_<tool>)`), a body that calls `RetailMcp.write`, a call builder that fixes the
argument names, the `WRITE` annotations and upstream's description plus
`CONFIRMATION_NOTE`. The parameters' descriptions are upstream's, shortened as in M9.
`get_item_details` is a catalog read: no data slice (it is in `CATALOG_TOOLS`), annotated
read-only.

The two-step nature of some upstream rules is kept, not hidden: an order can have its
items modified once (it becomes `pending (item modified)`), and a delivered order can be
returned or exchanged once. The descriptions say so, as upstream's do.

### The form summary

Today the form lists arguments: `new_item_ids: ["1008292230"]`. A person cannot tell from
that what they are agreeing to. The agent explains an exchange in chat before asking; over
MCP the client's model may or may not, and the form is the only text the server controls.

`propose` builds a summary for the four new writes, after the guard's preview has said the
only thing missing is the confirmation, so it is built only for the caller's own order.
It reads through the session's cached domain, as the tools do, and records nothing:

| Tool | Summary |
|---|---|
| `exchange_delivered_order_items`, `modify_pending_order_items` | Per item: product, the options that change (old → new), old and new price. Total difference, and whether it is charged to or refunded to the payment method. |
| `modify_pending_order_address` | The current address → the new one. |
| `modify_pending_order_payment` | The current payment method → the new one, and the order total. |

- The message is the summary, then `describe()` unchanged. The argument list stays the
  authoritative statement of what runs; the summary explains it.
- The fingerprint is still the arguments'. The summary is not stored and does not take
  part in the decision.
- If a value cannot be read (an unknown item id, say), the summary says so in that line
  and the form is still shown: refusing is the guard's and upstream's job, not the
  summary's. Upstream then refuses the call when it runs.
- Payment methods appear by id, as in the arguments. Balances and card details are not
  shown: `payment_methods` is one of the fields a redacting scope may hide
  (`SENSITIVE_FIELDS`), and the summary does not need them.
- Prices are read at the time of the question; the charge is computed by upstream when the
  call runs. In the demo nothing changes prices between the two, and the summary says the
  amounts are as of now.
- `cancel_pending_order` and `return_delivered_order_items` keep their current form. A
  summary for them is possible but not needed: their arguments are the order and a reason
  or the returned items, which the user named.

## Steps

Branch `feature/mcp-writes`, one commit per step.

1. **`get_item_details`.** Registered as a catalog read. Tests: listed with upstream's
   parameters, readable by any account, read-only annotations.
2. **The four writes.** Tools, resolvers, call builders, descriptions. Tests per tool, in
   both protocols: accepted executes once and a repeat replays; declined runs nothing and
   leaves nothing pending; another customer's order is `denied_ownership` with no form;
   a client without form elicitation gets `elicitation_unsupported`; the tool's parameters
   are upstream's (the existing check covers every exposed tool).
3. **The summary.** A pure function from the order and the catalog to text, tested on the
   pinned data: an exchange with a price increase and one with a refund, an item change,
   an address change, a payment change, an unknown item id. Then `propose` uses it, tested
   through the server: the form's message starts with the summary and still contains
   `describe()`.
4. **Documentation.** README's tool table and contract; the change record.
5. **Release (your hands).** Image `m11.1`, the demo plan (expected: 1 to change, the image
   parameter), `demo.py start` or `restart`. I prepare and check everything up to these
   commands.
6. **Acceptance on AWS**, then the change record's AWS section.

## Checks before starting

- [ ] Upstream's tool descriptions for the four writes, read from the pinned version, so
      the MCP descriptions match what the agent sees.
- [ ] How a failed upstream write is reported today. A domain error comes back as
      `Tool call failed: ...` content and `Store.execute_once` records it like a result.
      M11 keeps that behaviour and documents what the client sees.
- [ ] The demo data for the AWS rows: `ava` has one payment method (a gift card), so the
      payment change is tested locally on an account with two; on AWS the rows use the
      address change on `#W4135875` (pending) and an exchange on `#W4817420` (delivered).

## Tests and acceptance

CI rows run in CI; AWS rows run once after the release and are recorded in the change
record. No model is called in any row.

| Where | Scenario | Expected | Data recorded |
|---|---|---|---|
| CI | The M9 acceptance table and the access-case replay | unchanged: all pass in both protocols, the same counts | the JSON report |
| CI | Each new write, both protocols: accept, repeat, decline, another customer's order, no elicitation | `executed` once then `replayed`; `declined` with nothing pending; `denied_ownership` with no form; `elicitation_unsupported` | - |
| CI | A write outside the status slice when one is configured (an item change on a delivered order) | `denied_slice`, no form | - |
| CI | The summary on the pinned data, the six cases of step 3 | the expected lines | - |
| CI | `describe()` and the fingerprint of the M9 writes | unchanged | - |
| AWS | Tools listed with a real token | 12 | - |
| AWS | Claude Code: an address change on `#W4135875`, declined, then accepted | the form shows the old and new address; `declined`, then `executed` once | operations before and after |
| AWS | Claude Code: an exchange on `#W4817420` | the form shows products, options and the difference; `executed` once | operations before and after |
| AWS | `get_item_details` for an exchange's new item | the variant, with no data slice | - |
| AWS | The existing demo: sign-in, `/health` reports `m11.1` | unchanged | - |
| AWS | Rollback to `m10.1` | 7 tools listed | `demo.py status` |

The accepted writes change `ava`'s orders in the demo database: `demo.py archive` before
and a restore of that archive after, with `mcp` stopped around the restore as
[Operations](OPERATIONS.md#database-archives-and-restore) describes.

## Cost

No new resource and no model call. About two hours of `t3.small` for the release and the
acceptance, and one more image tag under the existing lifecycle policy.

## Decisions

Agreed on 2026-10-10.

1. **Tools: the four order writes and `get_item_details`.** `modify_user_address` stays
   unregistered: it changes the account, not an order, and nothing in the three business
   flows needs it.
2. **The form gets a summary, in the MCP server only.** `describe()`, the fingerprint and
   `WriteGuard` do not change, so the API and the evaluations are unaffected.
3. **Released to AWS** through M10's path, with the acceptance above.
