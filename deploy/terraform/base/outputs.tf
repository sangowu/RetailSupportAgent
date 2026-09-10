// What the demo layer needs to know about this one. It reads these through a remote state
// data source rather than being told them again, so the two layers cannot disagree.

output "ecr_repository_url" {
  description = "Where the demo layer's instance pulls its image from"
  value       = aws_ecr_repository.app.repository_url
}

output "bucket" {
  description = "State and archives. The demo layer's state lives under demo/ and archives under archives/."
  value       = data.aws_s3_bucket.state.id
}

output "instance_profile_name" {
  description = "The profile the demo instance runs under"
  value       = aws_iam_instance_profile.instance.name
}

output "model_key_parameter" {
  description = "Where the instance reads the model key from. The name, never the value."
  value       = var.model_key_parameter
}

output "demo_domain" {
  description = "The name the demo answers on"
  value       = aws_route53_zone.demo.name
}

output "demo_zone_id" {
  description = "Where the instance writes its own A record at boot"
  value       = aws_route53_zone.demo.zone_id
}

output "delegate_these_name_servers" {
  description = <<-EOT
    Add these as NS records for the subdomain, at the registrar of the parent domain.
    One manual step, once. Until it is done the name resolves to nothing and no
    certificate can be issued.
  EOT
  value       = aws_route53_zone.demo.name_servers
}
