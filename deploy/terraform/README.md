# Terraform deployment guide

The AWS demonstration environment has two Terraform layers. The base layer manages shared
resources, including ECR, Cognito, IAM, Route 53 and the identity parameter. The demo layer
manages EC2, its security group and rules, and the deployed image parameter. The S3 bucket
for state and archives is created separately so that destroying either layer does not
delete its recovery data.

Routine start and stop operations preserve the instance and its database. Provisioning and
teardown are separate operations. This is a single-instance demonstration environment;
PostgreSQL is stored on the instance's root volume.

## Prerequisites

Use Terraform 1.10 or later, AWS CLI credentials for the intended account, Docker and a
configured domain delegation. Install the project's Python dependencies for the operator
commands. The command examples use Bash and assume the repository root as the working
directory. For PowerShell, use equivalent environment-variable syntax; the Terraform
variable files shown below are shell-independent.

The examples use `eu-west-1`. Keep the backend region and both Terraform layers consistent.
Do not commit credentials, state, plan files or deployment-specific variable files.
Provisioning and running AWS resources incur charges.

## State bucket and base layer

Create the state bucket once per account:

```bash
REGION=eu-west-1
BUCKET="rsa-$(aws sts get-caller-identity --query Account --output text)"

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
aws s3api put-bucket-versioning --bucket "$BUCKET" --region "$REGION" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

terraform -chdir=deploy/terraform/base init \
  -backend-config="bucket=$BUCKET" \
  -backend-config="key=base/terraform.tfstate" \
  -backend-config="region=$REGION" \
  -backend-config="use_lockfile=true"
```

Create an untracked `deploy/terraform/base/terraform.tfvars` with the actual deployment
values. The repository ignores files named `terraform.tfvars`.

```hcl
state_bucket = "rsa-<account-id>"
region       = "eu-west-1"
demo_domain  = "rsa.example.com"
```

If Terraform should manage the parent domain's Cloudflare delegation, also configure
`cloudflare_zone_id` and provide `CLOUDFLARE_API_TOKEN` through the environment. Otherwise,
configure delegation manually as described below.

```bash
terraform -chdir=deploy/terraform/base plan
terraform -chdir=deploy/terraform/base apply
```

Review the complete plan and the account definitions before applying. The backend uses
S3 state locking through `use_lockfile`. Retain the bucket and its version history after
tearing down a demo.

## Model credentials and user accounts

Store the model API key separately from Terraform. With `DASHSCOPE_API_KEY` already set
in the shell environment:

```bash
aws ssm put-parameter --region eu-west-1 \
  --name /rsa/demo/dashscope-api-key --type SecureString \
  --value "$DASHSCOPE_API_KEY"
```

The instance retrieves this parameter at startup. Terraform receives only the parameter
name. Avoid passing secret values through Terraform variables, which can be recorded in
state.

Terraform creates Cognito users without assigning passwords. Inspect the generated
commands and set a password for each intended account:

```bash
terraform -chdir=deploy/terraform/base output set_these_passwords
```

Account definitions are controlled by `demo_accounts`. Changes require a reviewed
base-layer apply and an application restart to load the updated identity mapping. New
accounts also require password setup. See [account administration](../../docs/OPERATIONS.md#account-administration).

## Domain delegation

The base layer creates the Route 53 subdomain zone. If delegation is managed manually,
obtain its name servers and add the corresponding NS records at the parent DNS provider:

```bash
terraform -chdir=deploy/terraform/base output delegate_these_name_servers
```

Verify the chosen hostname's delegation before starting the demo. For the repository's
configured demonstration domain:

```bash
nslookup -type=NS rsa.proshot.top 8.8.8.8
```

The instance writes its A record at startup. Public DNS delegation must be correct for
hostname resolution and ACME certificate validation.

## Publish an image

Fetch the pinned benchmark data before building. Select a new release tag: the ECR
repository uses immutable tags and rejects attempts to overwrite an existing tag.

```bash
uv run python scripts/fetch_tau2.py
REGISTRY=$(terraform -chdir=deploy/terraform/base output -raw ecr_repository_url)
IMAGE_TAG="<unique-release-tag>"
aws ecr get-login-password --region eu-west-1 \
  | docker login --username AWS --password-stdin "${REGISTRY%%/*}"
docker build -t "$REGISTRY:$IMAGE_TAG" .
docker push "$REGISTRY:$IMAGE_TAG"
```

Use a verified application revision and record the image tag and digest. Retain images
needed for the accepted baseline, current deployment and rollback.

## Provision the demo layer

Initialize the demo backend using the existing state bucket:

```bash
BUCKET=$(terraform -chdir=deploy/terraform/base output -raw bucket)
terraform -chdir=deploy/terraform/demo init \
  -backend-config="bucket=$BUCKET" \
  -backend-config="key=demo/terraform.tfstate" \
  -backend-config="region=eu-west-1" \
  -backend-config="use_lockfile=true"
```

Create an untracked `deploy/terraform/demo/terraform.tfvars` with the actual bucket,
published image tag, ACME contact email and allowed HTTPS source addresses:

```hcl
state_bucket  = "rsa-<account-id>"
image_tag     = "<published-release-tag>"
acme_email    = "operations@example.com"
allowed_cidrs = ["203.0.113.4/32"]
```

Replace every placeholder, including the example source address, before planning.
Without an override, `allowed_cidrs` defaults to `0.0.0.0/0`; authentication still applies
to application access. An empty list disables external HTTPS access. Port 80 remains
available for ACME validation and HTTPS redirection. The contact email is supplied to
ACME for certificate administration.

```bash
terraform -chdir=deploy/terraform/demo plan
terraform -chdir=deploy/terraform/demo apply
terraform -chdir=deploy/terraform/demo output url
uv run python scripts/demo.py status
```

Initial startup installs Docker, pulls the image, runs migrations, seeds identities and
starts the application and HTTPS proxy. Allow for initialization and verify application
health. For subsequent operation:

```bash
uv run python scripts/demo.py start
uv run python scripts/demo.py stop
```

A stop preserves the database volume. The hostname remains stable, while its A record is
updated when the instance receives a different public IP.

## Release and rollback

Update `image_tag` in the demo variable file, retaining the other deployed values. Review
the full plan before applying, then restart the application stack:

```bash
terraform -chdir=deploy/terraform/demo plan
terraform -chdir=deploy/terraform/demo apply
uv run python scripts/demo.py restart
uv run python scripts/demo.py status
```

When the image tag is the only difference, the plan updates one Parameter Store value
without replacing the instance. Embedded script changes can also propose instance
replacement and deletion of its database volume. Follow the
[replacement procedure](../../docs/OPERATIONS.md#instance-replacement-and-user-data-differences)
before applying any such plan. The [reconciliation record](../../docs/USER_DATA_RECONCILIATION.md)
documents the resolved M7 user-data discrepancy.

The parameter value may be redacted in plan output. Use `demo.py status` to compare the
configured and running images after restart. A rollback target must contain the packaged
Compose configuration and Caddyfile, available from M6.8 onward, and remain compatible
with the database schema. Cross-schema rollback has not been validated.

## Teardown

Teardown deletes the demo instance and its root volume. Create and verify a database
archive before proceeding; the automatic shutdown archive can fail and must not be the
only recovery assumption.

```bash
uv run python scripts/demo.py archive
uv run python scripts/demo.py status
terraform -chdir=deploy/terraform/demo plan -destroy
terraform -chdir=deploy/terraform/demo destroy
uv run python scripts/aws_inventory.py
```

These Terraform commands use the same local variable file as provisioning. Review the
destroy plan and retain the base layer, state bucket, required archives and rollback
images. The inventory command checks which resources remain; it does not remove them.

## Deployment boundaries

Terraform is run by an operator. GitHub Actions currently runs verification and does not
provide an AWS deployment workflow or deployment OIDC role. Runtime permissions are
scoped through the instance IAM role. The instance reads credentials from Parameter
Store, and deployment-specific variables remain outside version control.
