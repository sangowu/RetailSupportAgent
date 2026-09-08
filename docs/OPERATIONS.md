# Local operation and on-demand AWS demo

Status: the three local start/stop scripts are implemented and verified by running them
(see [M0.5 part two](M0.5_LOCAL_DEMO.md)). The AWS part is not implemented; everything
about AWS below is an operational contract to be implemented and verified, not a set of
commands that work today.

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
