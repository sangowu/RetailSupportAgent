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
