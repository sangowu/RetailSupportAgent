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
    Addresses that may reach the demo over TLS.

    Open by default since M6.7. What an unauthenticated visitor can reach is a sign-in
    form; every route behind it requires a token the user pool signed, and what the holder
    may do is read from the database. An address list is still available and still worth
    setting for an environment nobody outside is meant to open.

    An empty list closes it to everyone, which remains the right way for this to fail.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
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

variable "acme_email" {
  description = <<-EOT
    Where Let's Encrypt sends expiry warnings. It is published in the certificate's
    issuance records, so use an address that is meant to be seen.
  EOT
  type        = string
}

variable "settings" {
  description = <<-EOT
    Application settings for the running service, written into the instance's environment
    file and passed through to the API container.

    This exists because "configurable" has to mean configurable where the service runs.
    The approval window, the session cache ceiling and the access scope all read an
    environment variable and none of them could be set on the instance: the compose file
    names every variable the container is given, and they were not in it. An operator's
    only option was to edit a file baked into the image.

    Names only, values as strings, e.g. `{ RSA_APPROVAL_TTL_SECONDS = "3600" }`. Leave it
    empty to run every setting at the application's own default, which is what the demo
    does.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for name in keys(var.settings) : startswith(name, "RSA_")])
    error_message = "Only RSA_ settings belong here. Anything else is a variable this file does not own."
  }

  validation {
    // The layer derives these from the base state and the image tag. Accepting an
    // override here would let a value set in two places disagree, and the one that won
    // would depend on the order compose read them in.
    condition = length(setintersection(keys(var.settings), [
      "RSA_DATABASE_URL",
      "RSA_IMAGE",
      "RSA_AUTH_ISSUER",
      "RSA_AUTH_AUDIENCE",
      "RSA_IDENTITIES",
      "RSA_REGION",
      "RSA_REGISTRY",
    ])) == 0
    error_message = "That setting is derived by this layer. Change it where it is derived, not here."
  }
}
