variable "region" {
  description = "AWS region. Must match the base layer's."
  type        = string
  default     = "eu-west-1"
}

variable "name_prefix" {
  description = "Prefix for every name this layer creates. Must match the base layer's."
  type        = string
  default     = "rsa"
}

variable "state_bucket" {
  description = "The base layer's bucket, where its outputs are read from. `terraform -chdir=deploy/terraform/base output bucket`."
  type        = string
}

variable "image_tag" {
  description = <<-EOT
    Which published image to run. An exact tag, never `latest`: the registry is set to
    IMMUTABLE tags so that two runs of the same tag are the same image, and a rollback is
    changing this value and applying.
  EOT
  type        = string

  validation {
    condition     = var.image_tag != "latest"
    error_message = "Use an exact tag. `latest` moves, and a demo whose image changes underneath it cannot be compared with the run before it."
  }
}

variable "allowed_cidrs" {
  description = <<-EOT
    Addresses that may reach the API and the UI. There is no default and an empty list
    closes it to everyone, which is the right way for this to fail.

    Until M6.5 adds HTTPS this carries a session cookie over plain HTTP, so it should be
    one address, not a network.
  EOT
  type        = list(string)
  default     = []
}

variable "instance_type" {
  description = "M6.2 measured 675 MB of 1913 MB in use. A t3.micro's 1 GiB does not fit once the operating system and Docker are counted."
  type        = string
  default     = "t3.small"
}

variable "volume_size" {
  description = "Root volume in GiB. M6.2 measured 3.2 GB in use; M6.1's 1 USD idle threshold breaks above 11."
  type        = number
  default     = 10

  validation {
    condition     = var.volume_size >= 8 && var.volume_size <= 11
    error_message = "Below 8 the stack does not fit; above 11 the idle cost passes M6.1's threshold and that is a decision to re-agree, not to make here."
  }
}

variable "idle_minutes" {
  description = "Continuous idleness before the instance stops itself."
  type        = number
  default     = 15
}

variable "max_uptime_minutes" {
  description = "Stop regardless after this long. Bounds the cost of the idle verdict being wrong."
  type        = number
  default     = 240
}

variable "compose_plugin_url" {
  description = "Pinned rather than 'latest': the plugin version is part of what was tested."
  type        = string
  default     = "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64"
}
