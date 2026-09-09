// The demo layer: one instance, and what it needs to be reachable.
//
// M6.1 chose a shape where an idle instance is stopped, not destroyed, so `apply` and
// `destroy` bracket the life of an environment rather than a demonstration. Between them
// the instance is stopped by the idle watcher and started by an operator, and Terraform
// does not track its power state.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "RetailSupportAgent"
      Layer   = "demo"
    }
  }
}

// The base layer's outputs, read rather than repeated, so the two cannot disagree about
// which registry or which bucket.
data "terraform_remote_state" "base" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "base/terraform.tfstate"
    region = var.region
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

// Resolved rather than pinned. A pinned AMI goes stale and stops receiving patches; the
// instance is recreated rarely enough that a newer image between applies is welcome.
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-x86_64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

// ---- who may reach it ------------------------------------------------------

resource "aws_security_group" "demo" {
  name        = "${var.name_prefix}-demo"
  description = "RetailSupportAgent demo: the API and the UI, from named addresses only"
  vpc_id      = data.aws_vpc.default.id

  tags = { Name = "${var.name_prefix}-demo" }
}

// No default. An empty list means nobody can reach it, which is the right way for this to
// fail: a demo nobody can open is a smaller problem than one anybody can.
resource "aws_vpc_security_group_ingress_rule" "api" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.demo.id
  description       = "API"
  cidr_ipv4         = each.value
  from_port         = 8000
  to_port           = 8000
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "ui" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.demo.id
  description       = "Demo UI"
  cidr_ipv4         = each.value
  from_port         = 7860
  to_port           = 7860
  ip_protocol       = "tcp"
}

// Outbound is open: the instance pulls its image, reads Parameter Store and calls the
// model API. There is no useful narrower rule, since the model endpoint is a public
// service behind addresses that change.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.demo.id
  description       = "Registry, Parameter Store and the model API"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

// ---- the instance ----------------------------------------------------------

locals {
  registry = split("/", data.terraform_remote_state.base.outputs.ecr_repository_url)[0]

  cloud_init = templatefile("${path.module}/../../instance/cloud-init.sh.tftpl", {
    compose_plugin_url  = var.compose_plugin_url
    region              = var.region
    registry            = local.registry
    image               = "${data.terraform_remote_state.base.outputs.ecr_repository_url}:${var.image_tag}"
    model_key_parameter = data.terraform_remote_state.base.outputs.model_key_parameter
    idle_minutes        = var.idle_minutes
    max_uptime_minutes  = var.max_uptime_minutes

    compose_yaml     = filebase64("${path.module}/../../instance/compose.yaml")
    rsa_start        = filebase64("${path.module}/../../instance/rsa-start.sh")
    rsa_service      = filebase64("${path.module}/../../instance/rsa.service")
    idle_shutdown    = filebase64("${path.module}/../../instance/idle_shutdown.py")
    rsa_idle_service = filebase64("${path.module}/../../instance/rsa-idle.service")
    rsa_idle_timer   = filebase64("${path.module}/../../instance/rsa-idle.timer")
  })
}

resource "aws_instance" "demo" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.demo.id]
  iam_instance_profile   = data.terraform_remote_state.base.outputs.instance_profile_name

  // No Elastic IP. An address costs 0.005 USD an hour whether it is attached or idle,
  // which is more than the disk this shape was chosen for. The address changes across a
  // stop, and the entry point reads the current one.
  associate_public_ip_address = true

  // Gzipped, which cloud-init decompresses. EC2 caps user data at 16 KB, and the six
  // base64 file blobs are 16.4 KB before compression. base64 of text compresses well, so
  // this leaves plenty of room rather than being a squeeze past the limit.
  user_data_base64            = base64gzip(local.cloud_init)
  user_data_replace_on_change = true

  root_block_device {
    // M6.2 measured 3.2 GB in use with the stack running. Ten leaves room for a second
    // image during a rollback and keeps the idle cost at 0.88 USD a month, inside M6.1's
    // 1 USD threshold.
    volume_size = var.volume_size
    volume_type = "gp3"
    encrypted   = true

    tags = { Name = "${var.name_prefix}-demo" }
  }

  metadata_options {
    // IMDSv2 only. A demo that can be talked into fetching a URL should not be able to
    // read its own credentials out of the metadata service.
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  tags = { Name = "${var.name_prefix}-demo" }

  lifecycle {
    ignore_changes = [
      // Amazon publishes a new AL2023 image regularly, and a resolved AMI would otherwise
      // replace a working instance, and its database with it, on an unrelated apply.
      // Replacing the instance is a decision, made by tainting it on purpose.
      ami,

      // A stopped instance reports this as false, because the auto-assigned address is
      // released on stop. Terraform reads that as drift on a replacement-forcing
      // attribute and plans to destroy and recreate the instance, taking the database
      // with it. Since M6.1 chose a shape where an idle instance is stopped, that is the
      // normal state of this environment, and a routine apply would have wiped it.
      // Measured: the first plan against a stopped instance said "must be replaced".
      associate_public_ip_address,
    ]
  }
}
