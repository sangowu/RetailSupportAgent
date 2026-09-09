# Creating and destroying the demo environment

Two layers. The base layer holds what a teardown must not take with it: the image registry
and the role the instance runs under. The demo layer holds the instance. The bucket that
keeps the state and the archives sits outside both, for the reason in the next section.

Between `apply` and `destroy` the instance is **stopped**, not destroyed. Applying takes
minutes and pulls a 722 MB image; starting a stopped instance takes 41 seconds, and that is
the demo path. Terraform does not track an instance's power state, so a stopped instance
does not show as drift.

## Once per account

The bucket that holds Terraform's state is not a Terraform resource. It cannot hold the
state of its own creation, and every way around that is a bootstrap dance. Three commands
create it instead, and both layers then have a real backend from their first `init`.

```bash
REGION=eu-west-1
BUCKET="rsa-$(aws sts get-caller-identity --query Account --output text)"

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
# Versioning, so a truncated state can be rolled back to the version before it.
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Then the base layer, which creates the registry and the role the instance runs under:

```bash
cd deploy/terraform/base
terraform init \
  -backend-config="bucket=$BUCKET" \
  -backend-config="key=base/terraform.tfstate" \
  -backend-config="region=$REGION" \
  -backend-config="use_lockfile=true"

terraform apply -var="state_bucket=$BUCKET"
```

`use_lockfile` is S3-native locking. DynamoDB-based locking is deprecated and would be a
second billed resource for no gain.

Last, put the model key where the instance can read it. Terraform is never told the value:
anything a variable carries ends up in state in clear.

```bash
aws ssm put-parameter --region "$REGION" \
  --name /rsa/demo/dashscope-api-key --type SecureString \
  --value "$DASHSCOPE_API_KEY"
```

## Publishing an image

The registry rejects a tag that already exists, so a tag names one image for good and a
rollback is applying an older tag.

```bash
REGISTRY=$(terraform -chdir=deploy/terraform/base output -raw ecr_repository_url)
aws ecr get-login-password --region eu-west-1 \
  | docker login --username AWS --password-stdin "${REGISTRY%%/*}"

docker build -t "$REGISTRY:m6.4" .
docker push "$REGISTRY:m6.4"
```

`scripts/fetch_tau2.py` has to have run first: the demo imports the benchmark's retail
domain, and the image copies it out of the verified checkout.

## Creating an environment

```bash
cd deploy/terraform/demo
BUCKET=$(terraform -chdir=../base output -raw bucket)

terraform init \
  -backend-config="bucket=$BUCKET" \
  -backend-config="key=demo/terraform.tfstate" \
  -backend-config="region=eu-west-1" \
  -backend-config="use_lockfile=true"

terraform apply \
  -var="state_bucket=$BUCKET" \
  -var="image_tag=m6.4" \
  -var='allowed_cidrs=["203.0.113.4/32"]'

terraform output urls
```

`allowed_cidrs` has no default and an empty list closes the environment to everyone. Until
HTTPS exists this carries a session cookie over plain HTTP, so it should be one address and
not a network.

The first boot takes about two minutes: installing Docker, pulling the image, running
migrations, starting both servers. After that the instance stops itself once nobody has
used it for fifteen minutes, and starting it again takes about forty seconds.

```bash
aws ec2 start-instances --instance-ids "$(terraform output -raw instance_id)"
terraform output -raw public_ip   # the address changes across a stop
```

## Destroying it

```bash
terraform -chdir=deploy/terraform/demo destroy -var="state_bucket=$BUCKET" -var="image_tag=m6.4"
uv run python scripts/aws_inventory.py     # confirm, do not assume
```

The base layer is not destroyed by that. Its bucket is not a Terraform resource at all, so
no `destroy` can reach the archives; removing them is an explicit `aws s3 rm`.

## What is deliberately not here

- **A `terraform.tfvars` in version control.** The addresses allowed to reach a demo are
  not a property of the project.
- **Anything holding the model key.** It reaches the instance from Parameter Store at boot.
- **GitHub Actions and OIDC.** Who runs Terraform is a separate question from what it
  describes, and is a later step.
