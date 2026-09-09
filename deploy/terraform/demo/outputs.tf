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

output "urls" {
  description = "Where the demo answers, for as long as it is running"
  value = {
    api  = "http://${aws_instance.demo.public_ip}:8000"
    ui   = "http://${aws_instance.demo.public_ip}:7860"
    idle = "http://${aws_instance.demo.public_ip}:8000/idle"
  }
}

output "image" {
  description = "The exact image this environment runs"
  value       = "${data.terraform_remote_state.base.outputs.ecr_repository_url}:${var.image_tag}"
}
