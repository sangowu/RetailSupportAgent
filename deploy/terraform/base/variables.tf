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
