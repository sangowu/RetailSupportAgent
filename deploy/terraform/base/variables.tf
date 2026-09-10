variable "region" {
  description = "AWS region. eu-west-1 is where every M6 measurement was taken."
  type        = string
  default     = "eu-west-1"
}

variable "name_prefix" {
  description = "Prefix for every name this layer creates. Also the S3 bucket's prefix, so it must be lowercase and DNS-safe."
  type        = string
  default     = "rsa"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,20}$", var.name_prefix))
    error_message = "Lowercase letters, digits and hyphens only: it becomes part of an S3 bucket name."
  }
}

variable "state_bucket" {
  description = <<-EOT
    The bucket holding both layers' Terraform state and, later, the archives.

    Created outside Terraform, once per account, because it cannot hold the state of its
    own creation. See deploy/terraform/README.md.
  EOT
  type        = string
}

variable "model_key_parameter" {
  description = <<-EOT
    SSM Parameter Store path holding the model API key, as a SecureString.

    Terraform is told the name and never the value: anything a variable carries ends up in
    state in clear. Put it there once, by hand:

      aws ssm put-parameter --name /rsa/demo/dashscope-api-key --type SecureString \
        --value "$DASHSCOPE_API_KEY" --region eu-west-1
  EOT
  type        = string
  default     = "/rsa/demo/dashscope-api-key"
}

variable "demo_domain" {
  description = <<-EOT
    The name the demo answers on. A subdomain delegated to this account's Route 53, not an
    apex: the apex stays with whatever hosts it today.

    After the first apply, add the name servers this outputs as NS records at the
    registrar of the parent domain. Nothing resolves until that is done.
  EOT
  type        = string
  default     = "rsa.proshot.top"
}

variable "cloudflare_zone_id" {
  description = <<-EOT
    The parent domain's zone id at Cloudflare, if that is where it is hosted. Given, the
    delegation is written as code; left empty, this layer creates nothing there and the
    records from `delegate_these_name_servers` are added by hand.

    The token comes from CLOUDFLARE_API_TOKEN in the environment, never from a variable: a
    variable would end up in state in clear. It needs Zone -> DNS -> Edit on that one zone
    and nothing else.
  EOT
  type        = string
  default     = ""
}

variable "demo_accounts" {
  description = <<-EOT
    The demo's accounts: the username someone signs in with, the retail identity that owns
    their orders, and the roles the database grants them.

    The roles are the ones `ROLE_PERMISSIONS` in src/retail_support_agent/auth.py defines.
    `demo` is not grantable here: it is the role the service invents for itself when
    authentication is off, and it carries session:reset_all.

    An empty `user_id` creates the account in the pool and provisions nothing, which is how
    "a token that verifies for a subject with no account here" stays a demonstrable case.

    Passwords are not here and are not in Terraform state. See `set_these_passwords`.
  EOT

  type = map(object({
    user_id = string
    roles   = list(string)
  }))

  default = {
    // Two customers, both with orders in the τ²-bench data, so one cannot reach the
    // other's is something to try rather than something to claim.
    ava  = { user_id = "ava_moore_2033", roles = ["customer"] }
    ivan = { user_id = "ivan_santos_6635", roles = ["customer"] }

    // Approvals and the audit trail, and no ticket:work: a supervisor decides and
    // assigns, and an approver who also performs the action is not an approver.
    sam = { user_id = "ava_moore_2033", roles = ["supervisor"] }

    // In the pool, not in the database. Signs in, and every action answers 403.
    nobody = { user_id = "", roles = [] }
  }

  validation {
    condition     = alltrue([for account in var.demo_accounts : !contains(account.roles, "demo")])
    error_message = "`demo` is the role the service invents when authentication is off, and it carries session:reset_all. It is not grantable to a real account."
  }
}

variable "image_parameter" {
  description = <<-EOT
    SSM Parameter Store path holding the image the demo runs. The demo layer writes it; the
    instance reads it at every start.

    Not in user data: user data runs once per machine, so a version written there can only
    be changed by replacing the machine, and that discards the database and the certificate
    with it. This is what makes a rollback a restart (M6.8).
  EOT
  type        = string
  default     = "/rsa/demo/image"
}

variable "identities_parameter" {
  description = <<-EOT
    SSM Parameter Store path holding the subject-to-identity mapping, read at every start.

    A String, not a SecureString. A subject is an opaque identifier; holding one
    authenticates nobody.
  EOT
  type        = string
  default     = "/rsa/demo/identities"
}
