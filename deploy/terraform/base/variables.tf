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
