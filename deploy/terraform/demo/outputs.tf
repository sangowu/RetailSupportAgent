output "instance_id" {
  description = "What the start and stop entry points act on"
  value       = aws_instance.demo.id
}

output "public_ip" {
  description = <<-EOT
    The current address. It changes across a stop, because this shape uses no Elastic IP:
    one costs 0.005 USD an hour attached or idle, more than the disk the shape was chosen
    for. Read it again after starting.
  EOT
  value       = aws_instance.demo.public_ip
}

output "url" {
  description = <<-EOT
    Where the demo answers. Unlike the address, this does not change across a stop: the
    instance rewrites its own A record at every boot.
  EOT
  value       = "https://${trimsuffix(data.terraform_remote_state.base.outputs.demo_domain, ".")}"
}

output "image" {
  description = "The exact image this environment runs"
  value       = "${data.terraform_remote_state.base.outputs.ecr_repository_url}:${var.image_tag}"
}
