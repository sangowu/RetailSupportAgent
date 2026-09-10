// The base layer: what a teardown must not take with it.
//
// The image registry and the bucket outlive any one demo environment. Destroying the demo
// layer removes an instance; destroying this removes the archives and the image, which is
// why it is a separate state and a separate decision.
//
// This layer is applied roughly once per account. It holds no secret: the model key lives
// in SSM Parameter Store, put there by hand, and Terraform is told only its name.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    // Only used to write the delegation. Configured from CLOUDFLARE_API_TOKEN in the
    // environment, so the token never becomes a Terraform variable and never enters state.
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }

  // The bucket this points at is not a Terraform resource. A bucket cannot hold the state
  // of its own creation, and every way around that is a bootstrap dance: local state that
  // is then migrated, or a backend block that has to be added after the first apply. Three
  // CLI commands in deploy/terraform/README.md create it instead, once per account, and
  // both layers then have a real backend from their first init.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  // Every resource this project creates carries the tag scripts/aws_inventory.py filters
  // on. Applied here rather than per resource, so a new resource cannot be added without
  // it and then not show up in the inventory.
  default_tags {
    tags = {
      Project = "RetailSupportAgent"
      Layer   = "base"
    }
  }
}

data "aws_caller_identity" "current" {}

// ---- the image registry ----------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = "${var.name_prefix}-app"
  image_tag_mutability = "IMMUTABLE" // a tag that moves makes two runs incomparable

  image_scanning_configuration {
    scan_on_push = true
  }
}

// Keep what a rollback needs and nothing else. AGENTS.md forbids deleting the first
// baseline, the current image and any rollback image; ten is comfortably more than that
// and stops an untagged layer pile-up from being paid for forever.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the ten most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

// ---- the state and archive bucket ------------------------------------------

// Created by hand, per deploy/terraform/README.md, and only read here. It holds this
// layer's own state, so Terraform managing it would mean a resource that can destroy the
// record of its own existence.
data "aws_s3_bucket" "state" {
  bucket = var.state_bucket
}

// ---- what the instance is allowed to do ------------------------------------

resource "aws_iam_role" "instance" {
  name = "${var.name_prefix}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name_prefix}-instance"
  role = aws_iam_role.instance.name
}

// Pulling the image. Read-only: the instance never pushes.
resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

// Session Manager, so the instance can be reached to be diagnosed without opening a port
// or putting a key on it.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

// The model key, and nothing else. Named parameters rather than a prefix wildcard on
// everything: this role should not be able to read another project's secrets.
resource "aws_iam_role_policy" "read_model_key" {
  name = "${var.name_prefix}-read-model-key"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.model_key_parameter}"
      },
      {
        // SecureString parameters are decrypted with the account's default SSM key.
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" }
        }
      },
    ]
  })
}

// Writing archives on stop, and reading them back. Confined to one prefix so a demo
// cannot overwrite the Terraform state that sits in the same bucket.
resource "aws_iam_role_policy" "write_archives" {
  name = "${var.name_prefix}-write-archives"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${data.aws_s3_bucket.state.arn}/archives/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = data.aws_s3_bucket.state.arn
        Condition = {
          StringLike = { "s3:prefix" = ["archives/*"] }
        }
      },
    ]
  })
}

// ---- the demo's name -------------------------------------------------------

// A delegated subdomain, not the apex. The apex stays wherever it is; only these NS
// records are added at the registrar, once, by hand. Until they are, this zone answers to
// nobody and no certificate can be issued.
resource "aws_route53_zone" "demo" {
  name    = var.demo_domain
  comment = "RetailSupportAgent demo; the A record is written by the instance at boot"
}

// The instance writes its own address here at every boot. Terraform does not own that
// record: it runs when someone applies, and the address changes when the machine starts.
resource "aws_iam_role_policy" "update_own_dns" {
  name = "${var.name_prefix}-update-own-dns"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["route53:ChangeResourceRecordSets"]
        Resource = aws_route53_zone.demo.arn
      },
      {
        // Waiting for the change to be accepted. Not scopable to one zone: the change id
        // is global.
        Effect   = "Allow"
        Action   = ["route53:GetChange"]
        Resource = "*"
      },
    ]
  })
}

// ---- the delegation itself -------------------------------------------------

// The parent domain's DNS is elsewhere, and the subdomain has to be pointed at the zone
// above. Doing that by hand would leave one remembered manual step in an environment whose
// whole point is being reproducible - and worse, a silent one: recreating the zone issues a
// different set of name servers, and records added by hand would go on pointing at the old
// ones.
//
// Optional. With no `cloudflare_zone_id` this creates nothing and the records are added
// wherever the parent is hosted; `delegate_these_name_servers` is what to add.
resource "cloudflare_dns_record" "delegation" {
  for_each = var.cloudflare_zone_id == "" ? toset([]) : toset(aws_route53_zone.demo.name_servers)

  zone_id = var.cloudflare_zone_id
  name    = trimsuffix(aws_route53_zone.demo.name, ".")
  type    = "NS"
  content = each.value
  ttl     = 3600
  comment = "RetailSupportAgent demo, delegated to Route 53 by deploy/terraform/base"
}

// ---- who may use the demo --------------------------------------------------

// A user pool, and nothing else about it. It issues tokens; it decides nothing. The
// token's `sub` is looked up in `identities` and `role_memberships` decides what the
// caller may do, so `cognito:groups` is deliberately never read and never written.
//
// Billing is per monthly active user with a large free allowance, so a pool with four
// accounts and an instance that is stopped most of the time adds nothing measurable to
// the idle cost M6.1 bounded.
resource "aws_cognito_user_pool" "demo" {
  name = "${var.name_prefix}-demo"

  // No self-registration. Accounts are created here and provisioned by the operator,
  // which is what stops "choose your own role at sign-up".
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  // Nothing is mailed: these accounts have no address, and a demo that cannot deliver a
  // message must not be configured as though it can.
  auto_verified_attributes = []

  // A teardown has to be able to remove this. Protection here would leave a pool behind
  // after `destroy` and the next apply would fail on the name.
  deletion_protection = "INACTIVE"
}

// The application the tokens are minted for. No secret: the client is the UI process,
// which holds no credential of its own and only passes the visitor's password through.
resource "aws_cognito_user_pool_client" "demo" {
  name         = "${var.name_prefix}-demo-ui"
  user_pool_id = aws_cognito_user_pool.demo.id

  generate_secret = false

  // Exactly two flows. Everything else - custom auth, admin sign-in, SRP - is absent
  // rather than unused, so a flow nobody reviewed cannot be reached.
  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  // A refusal must not say whether the username exists. This makes the pool answer
  // `NotAuthorizedException` for an absent user as well as a wrong password, which is
  // the same thing the UI would do on its own; both, because either alone is one change
  // away from leaking it.
  prevent_user_existence_errors = "ENABLED"

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

// The accounts themselves.
//
// No `password` and no `temporary_password`. Either argument writes the value into
// Terraform state, and this project's rule is that a credential lives in a secret manager
// or in the environment. Each user is created in FORCE_CHANGE_PASSWORD, and the operator
// runs one command per account to set a permanent one; `set_these_passwords` prints them.
resource "aws_cognito_user" "demo" {
  for_each = var.demo_accounts

  user_pool_id = aws_cognito_user_pool.demo.id
  username     = each.key

  // There is no address to send an invitation to.
  message_action = "SUPPRESS"

  lifecycle {
    // The operator sets the password out of band, which moves the user out of
    // FORCE_CHANGE_PASSWORD. Terraform must read that as done, not as drift to undo.
    ignore_changes = [password, temporary_password]
  }
}

locals {
  // The accounts that also get a row in `identities`. An account whose `user_id` is empty
  // is deliberately left unprovisioned: it exists in the pool and nowhere else, so that
  // "authenticated, and authorized to do nothing" is a case someone can sign in and see
  // rather than a paragraph in a document.
  provisioned = {
    for name, account in var.demo_accounts : name => account if account.user_id != ""
  }
}
