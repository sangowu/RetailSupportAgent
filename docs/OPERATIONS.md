# Local operation and on-demand AWS demo

Status: the three local start/stop scripts are implemented and verified by running them
(see [M0.5 part two](M0.5_LOCAL_DEMO.md)). The AWS part is not implemented; everything
about AWS below is an operational contract to be implemented and verified, not a set of
commands that work today.

The AWS shape named below - ECS Fargate behind an ALB, RDS, CloudFront - is superseded by
[M6.1](M6.1_DEPLOYMENT_SHAPE.md), which measured the candidates and chose a single
instance that is stopped when idle. The sections "AWS technical target", "Separate base
and demo layers" and "Free URL and HTTPS" are kept here unchanged until the step that
builds the Terraform rewrites them, so that what was assumed and what was measured stay
visible side by side. Read M6.1 first.

## Local start and stop

| Script | Behaviour | Status |
|---|---|---|
| `scripts/start-local.ps1` | Check dependencies and configuration, start the components this stage needs, wait for health, print the local URLs | implemented |
| `scripts/stop-local.ps1` | Stop this project's services, keeping data and experiment results | implemented |
| `scripts/reset-demo.ps1` | Reset an explicit simulated scope, deleting no baseline or other account's data | implemented |

"Control only processes confirmed to belong to this project" is implemented as: at
startup the PID and the process start time are both recorded under `.local/run/`, and
both must match before anything is stopped. On a mismatch the stop is refused and the
record kept. Checking the PID alone is not enough: Windows reuses PIDs, and the most
likely victim on this machine is an evaluation that runs for tens of minutes.

The scope of a reset is the demo sessions in process memory. M0's demo data lives
entirely in memory, each session holding its own database copy, so dropping sessions
restores the initial state. The script deletes no files and offers no way to.

M0 does not require PostgreSQL, Cognito or AWS to run; persistence is enabled later
through explicit local configuration. Model keys and Langfuse configuration are read
from an environment that is not under version control. First-time instructions cover the
Python, uv and Docker requirements, the configuration template, credential setup, the
simulated identity, the three demo flows and troubleshooting.

The Windows scripts control only processes and containers confirmed to belong to this
project and never terminate a global Python by fuzzy name. Background processes hide
their window, and a failed start reports the log location and what was cleaned up.

## The whole stack in containers (from M6.2)

The PowerShell scripts above run the application from a virtual environment on the
developer's machine, one process at a time. `compose.yaml` now also runs the whole stack -
PostgreSQL, migrations, the API and the demo UI - from one image built by `Dockerfile`.
This is the form the AWS demo runs in, so a problem found here is found before it is found
on an instance.

```
docker compose up -d --wait      # build if needed, start, wait for every health check
docker compose down              # stop, keeping the database volume
docker compose down -v           # stop and delete the data
```

- **The model key is read from the environment and has no default.** A stack that starts
  without a key and fails on the first message is worse than one that refuses to start.
- **Nothing is reachable from outside the machine** unless `RSA_BIND` is set to `0.0.0.0`.
  The default is the loopback address, the same as the local scripts.
- **Migrations are their own service** that runs once and exits, so two API containers
  starting together cannot run them at the same time.
- **`--wait` means health checks, not process starts.** Both servers are polled until they
  answer.

The repository's test suite can be run against the image's dependency set, which is what
shows that packaging changed no behaviour:

```
docker build --target test -t rsa-test:local .
docker run --rm -v "$PWD":/repo -w /repo   -e TAU2_DATA_DIR=/repo/third_party/tau2-bench/data   rsa-test:local /app/.venv/bin/python -m pytest -q
```

Building the image needs `third_party/` to exist, because the demo imports the benchmark's
retail domain at run time. Run `uv run python scripts/fetch_tau2.py` first; it verifies the
checkout by SHA256, and the image copies only the retail domain out of it.

## Starting and stopping a demo (from M6.5)

Three commands. They find the instance by its tags, so they work from any machine with
credentials, without Terraform's state.

```
uv run python scripts/demo.py status
uv run python scripts/demo.py start
uv run python scripts/demo.py stop
```

- **`start` waits for an answer, not for a state.** `running` means the hypervisor has the
  machine; the demo answers about half a minute later. Measured at 29 and 35 seconds.
- **`start` prints the URL.** From M6.6 that is one name over TLS and it does not change
  across a stop; the address underneath does, and the instance rewrites its own A record at
  every boot.
- **`stop` asks the application whether it is busy**, using the same verdict the idle
  watcher uses. It refuses while a write is awaiting confirmation, an approval is awaiting a
  human, or a ticket is open, and names which.
- **`stop --force` overrides** and prints what it overrode.
- **Stopping loses nothing.** The volume, the database and the image stay; the next start
  reads the same rows.

Exit codes: 0 done or nothing to do, 1 could not be done, 2 refused because work is
unfinished. A wrapper may retry on 1 and must not on 2.

## What the internet can reach (from M6.6)

The demo answers on one name over TLS, `rsa.proshot.top`, terminated by Caddy on the
instance. The certificate lives on a volume, so a stop and a start do not reissue it.

**Less is exposed than runs.** The UI is served at `/`, and `/health` and `/idle` are
proxied for the operator. The rest of the API is on the compose network and is not routable
from outside at all: `/chat`, `/tickets` and `/approvals` cannot be called even from an
address in `allowed_cidrs`. The UI builds its own agent in process and never calls the API
over the network, so nothing is lost.

| Port | Open to | What answers |
|---|---|---|
| 443 | `allowed_cidrs` | the demo |
| 80 | everyone | the ACME challenge and a redirect to HTTPS |

Port 80 cannot be restricted, because Let's Encrypt validates from wherever it happens to
be. Port 443 is what decides who can use the demo.

**A certificate is not a login.** Until authentication is on, `allowed_cidrs` is the only
thing deciding who may use this, and it should hold one address rather than a network.

The name is delegated once: see `deploy/terraform/README.md`. Until that is done the
instance still writes its A record at every boot and nobody can see it.

## Stopping the instance when nobody is using it (from M6.3)

M6.1 chose a shape whose idle cost is a stopped instance's disk. That number is only real
if something stops the instance, and `deploy/instance/` is that something.

| File | |
|---|---|
| `idle_shutdown.py` | asks the application whether it is idle, and stops the stack and then the machine |
| `rsa-idle.service` | runs it once, as root |
| `rsa-idle.timer` | every minute, starting two minutes after boot |

**Idle is what the application says it is, not what the CPU says.** A request blocked on a
model call uses almost no CPU, and a write waiting for confirmation looks like an empty
machine from outside. `GET /idle` answers with the verdict and, when it is not idle, what
is holding it open: writes awaiting confirmation, approvals awaiting a human, open tickets,
and time since the last request. The health and idle routes do not count as requests, or
the watcher's own polling would keep the machine alive.

Two independent reasons to stop, both configurable through `/etc/rsa-idle.env`:

| Variable | Default | |
|---|---|---|
| `RSA_IDLE_MINUTES` | 15 | continuous idleness before stopping |
| `RSA_MAX_UPTIME_MINUTES` | 240 | stop regardless, whatever the application says |
| `RSA_IDLE_URL` | `http://127.0.0.1:8000/idle` | where to ask |
| `RSA_IDLE_DRY_RUN` | unset | `1` logs the decision and changes nothing |

The cap is what bounds the cost of the first reason being wrong: one session that never
closes would otherwise keep a machine running until someone reads the bill. An application
that does not answer is never treated as idle, so a broken service stays up for someone to
look at, and the cap catches it.

To see the current verdict without acting on it:

```
python3 /opt/rsa/idle_shutdown.py --check
curl -s http://127.0.0.1:8000/idle
```

The instance is stopped, not terminated, so the disk, the database and the image survive
and the next start is 41 seconds rather than 122.

## AWS technical target

Region initially `eu-west-1`. Containers run on ECS Fargate with images in ECR;
application data and checkpoints in RDS PostgreSQL; secrets in Secrets Manager; runtime
logs in CloudWatch; baselines and archives in S3; GitHub Actions runs Terraform with
temporary OIDC credentials.

CPU, memory, concurrency and DB size are decided from local measurements before
deployment. The AWS move gets its own performance reference, so cross-region model
latency is not mistaken for a code regression.

## Separate base and demo layers

| Layer | Resources and rules |
|---|---|
| Base | ECR, S3 and the state file, secrets, Cognito, deployment IAM/OIDC, and any stable entry point required; long-lived with restricted permissions |
| Demo | ECS service, ALB, RDS, temporary networking; created and deleted on demand |

Two Terraform states, with dependencies output explicitly. The state store has
versioning, encryption, access control and a supported locking mechanism, and must not
be managed by the demo layer's destroy. Start and stop operations share one deployment
lock so an apply and a destroy cannot run at once.

Keeping the base layer does not mean zero cost. Images, objects and secrets incur
storage or service charges, and after shutdown the remaining resources are listed. No
promise is made that the bill drops to zero immediately. No budget cap is set, but
running time, leftovers and model experiment usage are recorded.

## Free URL and HTTPS

A free URL was chosen, so the stable CloudFront default `*.cloudfront.net` domain is
verified first, needing no purchased domain or separate Route 53 zone. It is a candidate
architecture, not a current configuration.

Before deploying, verify:

- The CloudFront plan and billing suit a dynamic API: cookie and Authorization
  forwarding, all required methods, caching disabled for personal data, SSE/streaming,
  and the Gradio paths.
- Cognito callback and logout URLs and FastAPI's external URL are correct, and proxy
  headers are trusted only from a trusted proxy.
- A CloudFront certificate covers browser to edge and does not encrypt edge to ALB by
  itself. An ALB default domain has no matching public certificate.
- If a VPC origin is used, verify availability, protocol and streaming limits, and cost.
  Free-tier support cannot be assumed.
- If a free entry point cannot provide the required protection with the current ALB
  architecture, propose an adjusted entry design with tests in M6 rather than silently
  falling back to sending credentials over public HTTP.
- The origin backend must not be reachable without login and resource authorization. A
  free URL does not mean public registration is allowed.

ACM and Route 53 are therefore not resources that must exist; whether they are needed
depends on the verified origin design. Do not introduce unverified security or cost
assumptions in order to show a free URL.

## AWS Start Demo

A manual GitHub Actions entry point, planned to take a release or image digest that has
passed the gates, a demo duration marker and an environment identifier. Order of
operations:

1. Verify the base layer, OIDC, secrets, the accounts allowed access, and the release
   manifest.
2. Allow only an image that passed the corresponding gate. No live retuning and no full
   model evaluation at this point.
3. Terraform plan, then apply the demo layer, recording the actual resource list.
4. Run database migrations and initialize the fixed simulated data. Starting an existing
   environment again must not reset its data.
5. Deploy the application, update the entry point, and verify HTTPS, login,
   authorization, streaming and a side-effect-free smoke test.
6. Print the URL, version, status and the stop entry point. On failure keep the
   diagnostics and clean up only this run's temporary resources, never an older
   environment.

One command is not instant. Creating RDS and networking takes time; M6 measures and
records cold-start and restart times. Allow preparation time before a demonstration and
keep the local version as a fallback.

## AWS Stop Demo

1. Stop accepting new work and wait for running operations to finish or reach an
   archivable state.
2. Export the demo audit, the state needed and the run manifest; upload to S3 and verify
   checksums and readability.
3. If archiving fails, stop the deletion and report. Never lose the only copy.
4. Terraform destroy the dedicated demo layer, including RDS, ALB, tasks and billed
   temporary networking resources.
5. Query the resource inventory to confirm cleanup, and print what the base layer keeps
   and what may still be billed.

Stopping a demo ends that demo environment, and the next start begins again from fixed
simulated data. That is different from a service restart recovering tasks within one
environment. Pausing RDS is not a long-term shutdown strategy: a stopped instance starts
itself again after 7 days and still incurs storage charges while stopped, and an ALB
stops costing only once deleted.

Source, images, baselines and archives must not be deleted along with a demo cleanup.
Image cleanup keeps the first baseline, the current version and any needed rollback.

## First-time setup and release acceptance

The first setup configures the AWS account and region, OIDC with minimal deployment
permissions, the S3 state backend, simulated accounts, the secrets in Secrets Manager,
and the Langfuse project. Cognito users are created by a controlled process with public
registration disabled.

Complete local checks and a concrete Terraform plan before any real deployment. Git
releases and the creation of billed resources happen under explicit authorization at the
time. Test rollback, a failed migration, a partially failed apply, an interrupted
destroy and a failed archive separately. Terraform manages resources, Actions sequences
the process, and Python handles migrations and data operations.

## Official references (checked during this discussion; re-check at implementation time)

- [GitHub manual workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-workflow)
- [GitHub OIDC to AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [Temporarily stopping RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_StopInstance.html)
- [Deleting an ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-delete.html)
- [CloudFront default domain and certificates](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesGeneral.html)
- [CloudFront pricing and plans](https://aws.amazon.com/cloudfront/pricing/)
