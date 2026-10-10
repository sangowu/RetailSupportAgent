# Operations guide

This guide describes the implemented local services and AWS demonstration environment.
The AWS deployment uses one EC2 instance in `eu-west-1`, with PostgreSQL on the instance,
Caddy for HTTPS, Cognito for authentication, ECR for images and S3 for database archives.
Earlier infrastructure proposals are retained in the historical project records; they do
not describe the deployed topology.

Run repository commands from the repository root. Commands for the instance filesystem
must be run on the instance. Provisioning instructions are in the
[Terraform guide](../deploy/terraform/README.md).

## Local services

Install dependencies with `uv sync --locked --group bench`, fetch the verified benchmark
data with `uv run python scripts/fetch_tau2.py`, and configure `.env` from `.env.example`.
The process scripts require PowerShell 7 and a configured model API key.

| Script | Behavior |
|---|---|
| `scripts/start-local.ps1` | Check prerequisites, start the API and UI, wait for health and print local URLs |
| `scripts/stop-local.ps1` | Stop the processes recorded by this project |
| `scripts/reset-demo.ps1` | Clear in-process demo sessions; does not delete database records or files |

```powershell
pwsh scripts/start-local.ps1
pwsh scripts/stop-local.ps1
```

The scripts record process IDs and start times under `.local/run/` and verify both before
stopping a process. Background windows are hidden. Startup failures report the log
location and clean up processes started by that invocation.

The default API and UI addresses are `127.0.0.1:8000` and `127.0.0.1:7860`. Without
authentication configuration, the service uses a fixed simulated identity and must remain
local. Without `RSA_DATABASE_URL`, session state is held in memory. Configure PostgreSQL
and run `uv run alembic upgrade head` to enable application persistence.

`reset-demo.ps1` is intended for the unauthenticated local demo. It sends no bearer token
and therefore cannot reset an authenticated deployment. With persistent storage, clearing
in-process sessions is not a database reset. Baselines, evaluation results and `.env` are
not modified by the reset script.

## Local container stack

The root `compose.yaml` starts PostgreSQL, a migration service, the API and the UI.
The AWS stack uses the separate `deploy/instance/compose.yaml` packaged in the image.

```bash
docker compose up -d --wait
docker compose down
```

`down` preserves the named database volume. Use `docker compose down -v` only when
intentionally deleting the local stack's volumes. The model key is required by the
container configuration. Published ports bind to loopback unless `RSA_BIND` is changed.
The migration service completes before application services start, and `--wait` checks
service health.

The image build requires the verified `third_party/tau2-bench` checkout. To run tests
using the image's dependency environment from a Bash shell:

```bash
docker build --target test -t rsa-test:local .
docker run --rm -v "$PWD":/repo -w /repo \
  -e TAU2_DATA_DIR=/repo/third_party/tau2-bench/data \
  rsa-test:local /app/.venv/bin/python -m pytest -m "not live_model" -q
```

Database tests additionally require a dedicated reachable test database and
`RSA_TEST_DATABASE_URL`; they are skipped without it. Local startup and health checks do
not call the model. Chat requests and live evaluations can incur provider charges.

## AWS start, status and stop

The operator commands locate the instance by project tags and use AWS credentials;
Terraform state is not required for these commands.

```bash
uv run python scripts/demo.py status
uv run python scripts/demo.py start
uv run python scripts/demo.py stop
```

`start` waits for application health and prints the HTTPS URL. Startup duration varies;
recorded timings are available in the M6 reports. The instance updates its DNS A record
at boot, so the hostname remains stable when its public IP changes.

`stop` checks application activity and refuses if confirmations, approvals or open tickets
remain. `stop --force` overrides the refusal and reports the outstanding work. Stopping
preserves the instance and its root volume. The shutdown service also attempts a database
archive; successful archiving must be verified separately.

| Exit code | Meaning |
|---|---|
| 0 | Completed, or no action required |
| 1 | Operation failed; inspect diagnostics before retrying |
| 2 | Refused because work is unfinished; resolve it or explicitly choose an override |

Starting EC2 incurs infrastructure charges. Storage and other retained resources may
continue to incur charges while the instance is stopped. A normal stop does not destroy
the Terraform environment or reset business data.

## Network access and authentication

Caddy serves the demo at `rsa.proshot.top`. The configured subdomain must be delegated to
Route 53 before public DNS resolution and certificate issuance can work.

| Port | Source | Service |
|---|---|---|
| 443 | `allowed_cidrs`; default `0.0.0.0/0` | HTTPS UI and the exposed health and idle endpoints |
| 80 | Public | ACME HTTP-01 validation and HTTPS redirect |

The instance API is reachable by the UI over the internal Compose network. Application
routes such as `/chat`, `/tickets` and `/approvals` are not forwarded by Caddy. The UI
performs Cognito sign-in on the server and retains tokens in its server-side session.
When authentication is enabled, application API routes require a valid token; `/health`
and `/idle` are unauthenticated operational endpoints.

Cognito establishes the caller's identity. The application maps the token subject through
`identities` and reads permissions from `role_memberships`. It does not grant business
roles from token group claims. See the [access model](ARCHITECTURE_AND_ACCESS.md).

## Account administration

Account definitions are in `deploy/terraform/base/variables.tf`. Terraform creates the
Cognito users and publishes the identity mapping. Passwords are set separately and are
not stored in the repository or Terraform configuration.

```bash
terraform -chdir=deploy/terraform/base output set_these_passwords
```

Run the generated password-setting commands with the intended account credentials. To
add an account, update `demo_accounts`, review and apply the base-layer plan, set its
password, and restart the application stack to apply identity seeding. Role changes must
also update that configuration and be applied before restarting; otherwise a manual
database change can be overwritten by the configured mapping on a subsequent start.

| Account | Retail identity | Role | Demonstration purpose |
|---|---|---|---|
| `ava` | `ava_moore_2033` | customer | Own-order access and action confirmation |
| `ivan` | `ivan_santos_6635` | customer | Cross-customer access restrictions |
| `sam` | `ava_moore_2033` | supervisor | Case assignment and approval decisions |
| `mia` | `ivan_santos_6635` | agent | Work on an assigned customer's case |
| `nobody` | No application identity | None | Refusal of an authenticated but unprovisioned account |

`sam` and `ava` intentionally share a retail identity while retaining separate account
subjects. Retail identity and authenticated account identity are distinct.

## Case management

The UI uses permissions returned by `/me` to select available sections. Every API action
also enforces permissions and resource access independently of UI visibility.

| Section | Required permission | Customer | Agent | Supervisor |
|---|---|---|---|---|
| Conversation | Authenticated application identity | Yes | Yes | Yes |
| Cases | `ticket:read` | No | Assigned cases | All cases |
| Work this case | `ticket:work` | No | Assigned cases | No |
| Assign | `ticket:assign` | No | No | Yes |
| Approvals | `escalation:decide` | No | No | Yes |

An agent works in a separate session associated with the ticket and their account. The
customer's conversation remains restricted to its owner. Resolving the case releases the
customer's session; the customer can continue by sending another message.

## Releases and rollback

Use an exact published image tag and the same deployment variables as the existing
environment. Review the full refreshed Terraform plan before applying it. The
[Terraform guide](../deploy/terraform/README.md) provides the commands.

When only the image tag changes, Terraform updates the image parameter in Parameter Store.
`uv run python scripts/demo.py restart` then restarts the application stack on the existing
instance. `demo.py status` reports both the configured and running versions; verify that
they match after the restart. The command checks for unfinished work before restarting.

The Compose configuration and Caddyfile are packaged with the image. Rollback targets must
be from M6.8 or later and must be compatible with the current database schema. Rollback
across schema changes has not been validated.

### Instance replacement and user-data differences

Instance scripts and systemd units are embedded in Terraform user data. With
`user_data_replace_on_change = true`, changing those inputs, including comments, can
propose replacing the instance. Its root volume contains PostgreSQL and is configured for
deletion on termination. Do not apply a replacement plan as a routine image update.

For an intentional replacement, create and verify an archive first, then validate the
restored data after provisioning. If an unrelated instance change must be deferred, a
reviewed targeted image-parameter apply can release the application independently:

```bash
terraform -chdir=deploy/terraform/demo apply -target=aws_ssm_parameter.image
```

This example assumes the required variables are supplied by the deployment's local
configuration. A targeted apply does not resolve the deferred user-data difference;
record it and review the next full plan.

A known metadata-only discrepancy can be reconciled by updating the stopped instance's
user-data attribute. Compare the complete decoded payload, retain the old value for
rollback, and verify a full plan after the update and refresh-only state synchronization.
Updating user data does not install or execute the scripts. Do not use it as a substitute
for a required runtime change, or suppress user-data differences through lifecycle rules
solely to obtain a clean plan.

The discrepancy identified after M7 was resolved on 2026-09-10. The final full plan showed
no changes, with the instance and root volume preserved. The installed script was not
re-inspected while the instance was stopped. Evidence is in the
[reconciliation record](USER_DATA_RECONCILIATION.md).

For the base layer, a full refresh of managed Cloudflare records requires
`CLOUDFLARE_API_TOKEN`. If a scoped account change is applied with resource targeting,
review the affected Cognito and identity-parameter resources and document the deferred
full-plan verification. Resource targeting is an exception, not the default release path.

## Database archives and restore

The shutdown service attempts to archive PostgreSQL to the S3 state bucket under
`archives/` before stopping the stack. Operators can also request an archive explicitly:

```bash
uv run python scripts/demo.py archive
uv run python scripts/demo.py status
uv run python scripts/demo.py restore latest
```

`restore` requires an explicit archive selection: `latest` or a specific archive key.
It replaces the database and refuses while work remains unfinished unless explicitly
overridden. API and UI services stop during restoration, and users must sign in again.

The instance's restore script predates the MCP server (M10) and does not stop it, though
it writes to the same database. Until the script is updated with the next instance
replacement, stop it by hand around a restore. With it stopped, `/idle` reports "the MCP
server did not answer", so the restore needs `--force`; check that this is the only
holding reason first:

```bash
# on the instance
docker stop rsa-mcp-1
# from the operator's machine
uv run python scripts/demo.py restore <archive key> --force
# on the instance
docker start rsa-mcp-1
```

Before a teardown, create an archive and verify its result and timestamp. The automatic
shutdown archive is bounded by a 120-second timeout; failure is logged but does not block
instance shutdown. Use `status` to inspect the latest archive and inspect
`journalctl -u rsa.service` on the instance when an archive is missing or stale.

Archives include application identity records. Restoring those records does not recreate
a deleted Cognito account. Archives currently have no automatic expiry policy. Retain
archives required for recovery or evaluation, and review storage use as they accumulate.
Caddy certificate data, including its private key, is not included in database archives.

## Automatic shutdown

`deploy/instance/idle_shutdown.py` checks application activity through `/idle`.
`rsa-idle.timer` runs the check every minute, starting two minutes after boot.
Confirmations, pending approvals, open tickets and recent activity prevent an idle verdict.
Health and idle polling do not count as user activity.

| Variable | Default | Purpose |
|---|---|---|
| `RSA_IDLE_MINUTES` | `15` | Required continuous idle period |
| `RSA_MAX_UPTIME_MINUTES` | `240` | Maximum runtime, including when work remains pending |
| `RSA_IDLE_URL` | `http://127.0.0.1:8000/idle` | Application activity endpoint |
| `RSA_IDLE_DRY_RUN` | Unset | Set to `1` to log decisions without stopping the instance |

A failed activity check is not treated as idle. The maximum-runtime limit still bounds
instance uptime. Inspect the current decision on the instance without taking action:

```bash
python3 /opt/rsa/idle_shutdown.py --check
curl -s http://127.0.0.1:8000/idle
```

## Operational validation limits

The current environment has no database replication or high availability. Complete browser
acceptance, concurrent-load testing and cross-schema rollback remain unverified. GitHub
Actions runs verification, not automated AWS deployment. The implemented operator commands
and Terraform configuration remain the deployment entry points.

Use the stage reports for historical measurements and the current guides for operational
procedures. Record the image, configuration and verification results for each deployment;
do not infer runtime correctness solely from a successful infrastructure plan.
