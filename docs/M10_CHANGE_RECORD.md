# M10: MCP server on the demo instance, change record

Status: **implemented, tested and rehearsed locally. Not released.** The plan is
[M10_MCP_DEPLOY.md](M10_MCP_DEPLOY.md). Section 2.5 is what remains: the release and the
acceptance on AWS, which are filled in when they have run.

## 1. Change card

Recorded on 2026-10-10 against `main` at `5fcf635`.

- Stage and step: M10, steps 1 to 5 of the plan, on branch `feature/mcp-deploy`.
- Type: feature, plus one fix released with it (#75, its own PR).
- Problem: the M9 MCP server runs only on a developer's machine; using it means starting
  it locally with a database and a pool configured.
- Single theme: run the same server on the demo instance, behind the existing proxy, with
  no new AWS resource.
- Controls not touched: model, prompt, reasoning settings, evaluations and baselines,
  `WriteGuard`, the agent path, the idle watcher and the instance's user data, Terraform.
  `Store` gains one method (`withdraw_confirmation`, decision 1 of the plan); no existing
  method changes.
- Expected benefit: an MCP client connects to `https://<demo domain>/mcp` with a pool
  token. Cost: one more container on a `t3.small`, about 300 MB with four sessions.
- Primary metric: the plan's acceptance table, CI and local rows here, AWS rows after the
  release.
- Protective metrics: no new failure in the existing suite; the M9 acceptance table
  unchanged in both protocols; the three baselines verify; the demo's own sign-in and
  chat unchanged after the release; at least 25% of the instance's memory free.
- Reference: the suite on `5fcf635`. No model score is involved, so the MVP comparison
  does not apply.

## 2. Implementation and verification

### 2.1 Commits

| Commit | What it does |
|---|---|
| `c249013` | Revises the plan: the idle answer moves into the API (section 5) |
| `f35665c` | Step 1: `mcp` image role; `RSA_MCP_ALLOWED_HOSTS` for the SDK's Host and Origin check; unauthenticated `/health` and `/idle` on the MCP app; the activity clock moves only on tool calls past authentication |
| `98335f6` | Step 2: the `mcp` service in both compose files (locally behind a profile), Caddy routes for `/mcp` and its protected-resource metadata |
| `30dcc64` | Step 3: the API's `/idle` asks the MCP server when `RSA_MCP_IDLE_URL` is set and reports the shorter idle time; no answer is a holding reason |
| `0248a5d` | Step 3: `RSA_MCP_IDLE_URL` in `.env.example`, which lists every setting |
| `ed14cc3` | Step 4: `Store.withdraw_confirmation`; a declined or cancelled MCP form leaves no pending confirmation |

Released with these, from its own PR: #75 (`5fcf635`), the idle watcher reaching the API
again (section 4).

### 2.2 Files

| File | Responsibility |
|---|---|
| `src/retail_support_agent/mcp_server.py` | Allowed hosts, operator routes, activity clock, withdraw on decline |
| `src/retail_support_agent/api.py`, `config.py` | `/idle` includes the MCP server; `ask_mcp_idle` |
| `src/retail_support_agent/store.py` | `withdraw_confirmation` |
| `src/retail_support_agent/mcp_acceptance.py` | The decline scenario also requires no pending confirmation left |
| `docker/entrypoint.sh` | The `mcp` role |
| `deploy/instance/compose.yaml`, `compose.yaml` | The `mcp` service; `RSA_MCP_IDLE_URL` for the API |
| `deploy/instance/Caddyfile` | `/mcp` and `/.well-known/oauth-protected-resource/mcp` to `mcp:8765` |
| `.env.example` | `RSA_MCP_IDLE_URL` |
| `tests/test_mcp_server.py`, `test_idle_shutdown.py`, `test_deploy_files.py`, `test_persistence.py`, `test_mcp_writes.py` | 39 new tests |

### 2.3 Checks before starting

1. **Does the Host check let Claude Code through the proxy?** Read in
   `mcp/server/transport_security.py`: a request without an `Origin` passes
   (`_validate_origin`), a wrong `Host` gets 421, a wrong `Origin` 403. Caddy forwards the
   original `Host`. Confirmed in the rehearsal through Caddy.
2. **Does Caddy stream the server's event streams?** Yes: both protocols completed reads
   and a form round trip through Caddy and TLS in the rehearsal (2.4).
3. **Memory on Linux.** Measured in the image (2.4).

### 2.4 Verification

**Tests.** Before: ruff, format and mypy pass; pytest 1125 passed, 0 failed. After: ruff,
format and mypy (103 files) pass; pytest 1164 passed, 0 failed, in 135 s; the three
baselines verify. Windows, PostgreSQL 17 in Docker.

**Compose and Caddy.** `docker compose config` accepts both files; locally `mcp` starts
only with `--profile mcp`. `caddy adapt --validate` accepts the Caddyfile and lists the
routes `/mcp`, `/.well-known/oauth-protected-resource/mcp`, `/health`, `/idle`.

**Rehearsal.** The image built as a release builds it (`retail-support-agent:m10-rehearsal`,
226 MB), run with the instance's own `compose.yaml` and `Caddyfile`. A scratch override
changed only Caddy's published port to `127.0.0.1:8443` and its Caddyfile mount, and
`RSA_DOMAIN=localhost:8443` made Caddy serve with its internal CA. The demo pool's real
issuer, client and identity mapping; no model key (a placeholder, and no message sent).
Every service came up healthy, `mcp` included. The token was `ava`'s access token from
the pool, read from a file, never printed, deleted afterwards.

| Check, through Caddy and TLS | Result |
|---|---|
| No token; altered signature; `sub` changed to `ivan`'s with the signature kept | 401, 401, 401 |
| `ava`'s token, legacy `initialize` | 200 |
| Protected-resource metadata | names `https://localhost:8443/mcp` and the pool |
| 2026-07-28: tools, own order, `ivan`'s order, a cancel declined | 7, `ok`, `denied_slice`, `declined` with one form; 3.7 s for the session |
| legacy: the same | 7, `ok`, `denied_slice`, `declined` with one form; 2.3 s |
| Pending confirmations after both declines | 0 |
| API `/idle` before and after the MCP calls | `seconds_since_mcp_request` 131.2, then 0.3; the API's own 130.2, then 136.4 |
| `idle_shutdown.py --check` on the host (#75) | `staying up: idle, 15 more minutes to go`: it reaches the API |
| The MCP app's `/health` and `/idle` from outside | not routed to it; `/health` and `/idle` at the proxy are the API's, as before |

**Memory**, `docker stats` on the rehearsal and a measurement inside the `mcp` container:

| | MB |
|---|---|
| `mcp` idle, before any session | 81 |
| `mcp` with one session | 261 |
| Each further session (measured on Linux in the image) | 12 |
| `mcp` with the pool's four accounts, projected | about 297 |
| `api`, `ui`, `db`, `caddy` in the rehearsal | 126, 138, 44, 20 |

M6.2 measured 675 MB in use of 1913 MB on the instance. Adding about 297 MB projects
about 990 MB, 48% free, against the 25% gate. The gate is decided on the instance.

### 2.5 Not yet run: the release and the AWS acceptance

The plan's steps 6 and 7. Every AWS row of the plan's table is pending.

## 3. Comparison

| Metric | Before | After | Conclusion |
|---|---|---|---|
| Tests passing | 1125 | 1164 | 39 added, none failing |
| M9 acceptance table, both protocols | 10/10 | 10/10 | unchanged; the decline row now also requires no pending confirmation |
| Images | M8's | one more role in the same image | no new image |
| AWS resources | - | none added | as planned |
| Task success, safety violations, P95, cost | not applicable | not applicable | no model is called by this change |

## 4. Failure analysis and conclusion

- **A defect in an earlier stage, found while planning.** Since M6.6 the idle watcher had
  no route to the API: it asks `127.0.0.1:8000` on the host, and M6.6 had stopped
  publishing that port. It reads no answer as "not idle", so the instance would stop
  only at the 240-minute cap. Fixed in #75 by publishing the API on the loopback address
  only, and released with M10. Observed working in the rehearsal; not yet on AWS.
- **The plan's watcher change was not buildable as a release.** The watcher is installed
  by user data, and changing it replaces the instance (M6.8). The idle combination moved
  into the API instead (section 5).
- **A test bug, not a code bug.** One new idle test failed on a fake clock shared across
  its class; the test was corrected to not depend on order.
- **Limits.** The rehearsal ran on Windows with Docker Desktop, not on the instance. The
  memory gate is a projection until measured there. `/idle` is reachable without a token
  at the proxy, as since M6.6; it now also carries `seconds_since_mcp_request`, a count.
  An MCP server that does not answer holds the instance up to the cap, by design.
- **Conclusion: PASS for implementation and local rehearsal; INCONCLUSIVE for the stage**
  until the release and the AWS rows have run.

## 5. Differences from the plan

| Plan | Built | Why |
|---|---|---|
| The watcher asks the API and the MCP server | The API's `/idle` asks the MCP server; the watcher is unchanged | The watcher is in user data; changing it replaces the instance |
| - | #75, the watcher reaching the API again, as its own PR | Found while revising the plan |
| Step 5: the M9 acceptance script against the containerised server | Checks with the pool's real token through Caddy and TLS | The script signs its own tokens and the container trusts only the pool; the script's scenarios run in CI on the same code |
| The `mcp` service in both compose files | Locally behind a `mcp` profile | A developer machine has no pool, and the server refuses to start without one |
| - | `.env.example` lists `RSA_MCP_IDLE_URL` | The template must list every setting the API reads |
