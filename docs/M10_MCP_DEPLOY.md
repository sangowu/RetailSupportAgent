# M10: the MCP server on the demo instance

> Plan, 2026-10-10. It describes what to build; nothing here exists yet. M9 built and
> accepted the server locally ([M9 change record](M9_CHANGE_RECORD.md)).

## Goal and scope

Serve the M9 MCP server from the demo instance, so that an MCP client such as Claude Code
connects to `https://<demo domain>/mcp` with a token from the demo user pool, without
anyone running a server locally.

**In scope:**

- An `mcp` service in the instance stack, from the same image, behind the existing Caddy.
- What the deployment needs that the local server did not: a Host check for the public
  name, a health check, and a share in the idle decision.
- One release through the existing path (image, image parameter, restart), and an
  acceptance run against the deployed server.

**Out of scope:**

- No new tools and no change to what a call is allowed to do. `WriteGuard`, `Store` and
  `AccessScope` decide exactly as in M9.
- No OAuth flow. The client is given an access token as a static header, as in M9; the
  pool's hosted sign-in would need a domain and callback URLs, which is a separate decision.
- No new AWS resource. If a step turns out to need one, it stops and is agreed first.

## What exists

| Part | Today | What M10 does with it |
|---|---|---|
| `mcp_server.main()` | Starts on `RSA_MCP_HOST:RSA_MCP_PORT`, refuses to start without a database or a pool | Runs as the image's `mcp` role |
| `docker/entrypoint.sh` | Roles `api`, `ui`, `migrate`, `seed` | Adds `mcp` |
| `deploy/instance/compose.yaml` | `db`, `migrate`, `seed`, `api`, `ui`, `caddy`; ships inside the image (M6.8) | Adds `mcp`; the local `compose.yaml` gets the same service (`tests/test_deploy_files.py` requires both to agree) |
| `deploy/instance/Caddyfile` | Routes `/` to the UI, `/health` and `/idle` to the API, nothing else | Adds `/mcp` and the protected-resource metadata path |
| `/opt/rsa/.env` | Already holds `RSA_AUTH_ISSUER`, `RSA_AUTH_AUDIENCE`, `RSA_DOMAIN` | Read by the new service; no new value, so no user-data or Terraform change |
| Security group | 443 open to `0.0.0.0/0` since M6.7 | Unchanged; `/mcp` answers 401 before any tool without a pool-signed token |
| `idle_shutdown.py` | Asks the API's `/idle`; 15 idle minutes or 240 minutes of uptime stops the instance | Also asks the MCP server (see below) |
| Instance | `t3.small`, 1913 MB; M6.2 measured 675 MB in use | Unchanged if the measurement below holds |

## Design

### The service

One more process from the same image, as M6.2 chose for the API and the UI: a failing MCP
server is visible as its own container, and it cannot take the UI down with it.

```
internet ──443──▶ caddy ─┬─ /                                        ──▶ ui:7860
                         ├─ /health, /idle                           ──▶ api:8000
                         └─ /mcp, /.well-known/oauth-protected-resource/mcp ──▶ mcp:8765
                                                              │
                                    db (PostgreSQL) ◀─────────┴── api, ui, mcp
```

- `RSA_MCP_HOST=0.0.0.0`, `RSA_MCP_PORT=8765`, `RSA_MCP_URL=https://${RSA_DOMAIN}/mcp`.
  The port is not published; only Caddy reaches it.
- `RSA_SESSION_CACHE_SIZE` keeps its default. Sessions are cached per account and the pool
  has four, so at most four domains are held.
- A health check on a new unauthenticated `/health` route of the MCP app. Caddy does not
  route it; it is for compose and the operator.

### The Host check

The SDK turns DNS-rebinding protection on only when it binds to a loopback address
(`mcp/server/mcpserver/server.py`, lines 1163-1168). Bound to `0.0.0.0` in a container,
it accepts any `Host`. M10 adds a setting, `RSA_MCP_ALLOWED_HOSTS`, passed to the SDK's
`TransportSecuritySettings`; on the instance it is `${RSA_DOMAIN}`, so `/mcp` answers only
requests addressed to the demo's name. Unset, behaviour stays as in M9, so the local
default and the existing tests do not change.

### Idle

M6.3's rule is that the application says whether it is idle, and an unreachable
application is never idle. Two gaps appear once MCP runs on the instance:

1. **MCP traffic does not count as activity.** The API's `/idle` measures time since the
   API's own last request. Someone using only Claude Code would see the instance stop
   after 15 minutes.
2. **A declined MCP write holds the instance open.** A declined confirmation stays
   `pending` (an M9 limit, as on the agent path), and a pending confirmation is a holding
   reason, so one declined form keeps the instance up until the 240-minute cap.

The plan for each:

1. The MCP app gets an unauthenticated `/idle` that reports `seconds_since_request`,
   counted from the last **tool call that passed authentication**. A 401 does not count,
   or anyone on the internet could keep the instance running. The watcher asks both
   services and stops only when both are idle for 15 minutes; either unreachable means
   "do not know", as today. `demo.py` reads only the holding reasons, which come from the
   database and already include MCP sessions, so it needs no change.
2. **A decision for you** (below): close the pending confirmation when the user declines
   or cancels the form.

### Token lifetime

The pool's access tokens last 60 minutes (`deploy/terraform/base/main.tf`). With a static
header, the client has to be added again every hour. This plan keeps 60 minutes and
documents the re-add command. Lengthening it is a change to the base layer and to every
token the pool issues, including the UI's, and is not part of this stage.

## Steps

Branch `feature/mcp-deploy`, one commit per step.

1. **The role and the settings.** `mcp` in `entrypoint.sh`; `RSA_MCP_ALLOWED_HOSTS`; the
   MCP app's `/health` and `/idle`, with the activity clock moved to authenticated tool
   calls. Unit tests for each, including "a 401 does not reset the clock".
2. **The stack.** The `mcp` service in both compose files; the two Caddy routes;
   `tests/test_deploy_files.py` extended (the same service in both files, a health check,
   no key in the instance file, `/mcp` routed and the MCP `/idle` not routed).
3. **The watcher.** `idle_shutdown.py` asks both URLs; the decision stays a pure function
   and is tested with both answers in every combination.
4. **Confirmation on decline**, only if you choose it below.
5. **Local rehearsal.** The local compose stack with the new service, the image built as
   the release will build it, and the M9 acceptance script run against the containerised
   server. Memory measured with `docker stats`.
6. **Release (your hands).** Image `m10.1` built and pushed, `terraform apply` on the demo
   layer (expected: 1 to change, the image parameter), `demo.py restart`. I prepare and
   check everything up to these commands; you run them.
7. **Acceptance on AWS**, then the change record and the README.

## Checks before starting

- [ ] The Host check accepts Claude Code's requests through Caddy. Caddy forwards the
      original `Host`, which should match. Whether the SDK also checks `Origin` when a
      client sends none has to be read in `transport_security.py`.
- [ ] Caddy streams `text/event-stream` responses without buffering. It flushes them
      immediately by default; the 2026-07-28 protocol's input-required rounds and the
      legacy mid-call form both go through it.
- [ ] Memory on Linux in the image. The Windows measurement is about 270 MB for the
      process and 12 MB per cached session; the gate below decides.

## Tests and acceptance

Local rows run in CI. AWS rows run once, after the release, and are recorded in the change
record. No model is called in any row.

| Where | Scenario | Expected | Data recorded |
|---|---|---|---|
| CI | The M9 acceptance table | unchanged: all ten pass in both protocols | the JSON report |
| CI | Both compose files define `mcp` alike; `/mcp` routed; MCP `/idle` not routed | pass | - |
| CI | A 401 leaves the MCP idle clock alone; an authenticated call resets it | pass | - |
| CI | Watcher: API idle and MCP busy, MCP idle and API busy, either unreachable | stays up in each | - |
| Local | The acceptance script against the containerised server | all pass | the JSON report |
| Local | Memory with the full stack and four MCP sessions | the instance would keep at least 25% free | MB per container; total |
| AWS | No token; altered signature; `sub` swapped | 401, 401, 401 | status codes |
| AWS | A `Host` other than the demo's name | refused | status code |
| AWS | Real token: tools listed, own order, another customer's order | 7; `ok`; `denied_slice` | - |
| AWS | Claude Code: a cancel, the form declined, then the same cancel accepted | `declined`, then `executed` once | operations before and after |
| AWS | Read latency from this machine, 20 calls of `get_order_details` | recorded, not gated | p50, p95, as the first AWS MCP baseline |
| AWS | Only MCP used for 20 minutes | the instance stays up | the watcher's decisions from its journal |
| AWS | Nothing used for 15 minutes | the instance stops | time from last call to stop |
| AWS | The existing demo: sign-in, a chat turn, `/health` reports `m10.1` | unchanged | - |
| AWS | Rollback to the previous image | the stack starts without `mcp`; `/mcp` is not routed | `demo.py status` |

The accepted cancel changes `ava`'s pending order `#W4135875` in the demo database. The
plan is `demo.py archive` immediately before that row and `demo.py restore` of that
archive after it (M6.9), so the demo is left as it was.

## Cost

No new resource and no model call. The instance runs for the release and the acceptance,
about two hours of `t3.small`, and the idle test needs one natural stop. The ECR image
adds one tag under the existing lifecycle policy.

## Decisions for you

1. **Close a confirmation when the form is declined or cancelled?** Recommended: yes, for
   MCP only. It is a new `Store` method that sets the row to `cancelled`; existing methods
   do not change, and the agent path keeps its behaviour. Without it, a declined form keeps
   the instance up until the 240-minute cap, about 0.09 USD each time.
2. **The memory gate.** Recommended: at least 25% of the instance's memory free with the
   full stack and four MCP sessions. If it fails, the next size up is a cost change against
   M6.1's threshold and is brought back to you, not made here.
3. **Restoring the demo database after the accept test.** Recommended: yes, from an archive
   taken just before it.
