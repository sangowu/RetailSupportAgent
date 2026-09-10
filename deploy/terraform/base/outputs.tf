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

// ---- what the demo layer needs to authenticate anyone -----------------------

output "cognito_issuer" {
  description = "What the API verifies a token's `iss` against"
  value       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.demo.id}"
}

output "cognito_client_id" {
  description = "The app client. The UI signs in against it; the API checks it as the audience."
  value       = aws_cognito_user_pool_client.demo.id
}

output "cognito_user_pool_id" {
  description = "For the one command that sets a password, and for nothing else"
  value       = aws_cognito_user_pool.demo.id
}

output "demo_identities" {
  description = <<-EOT
    The subject-to-identity mapping, as the instance's RSA_IDENTITIES expects it. Subjects
    are opaque identifiers, not credentials: this decides nothing on its own, since the
    account still has to hold a token the pool signed.
  EOT
  value = jsonencode([
    for name, account in local.provisioned : {
      subject = aws_cognito_user.demo[name].sub
      user_id = account.user_id
      roles   = account.roles
    }
  ])
}

output "set_these_passwords" {
  description = <<-EOT
    One command per account, run once after the first apply. Terraform creates each user
    without a password, because both of its password arguments write the value into state.
    Choose a password per account and keep it wherever you keep passwords.
  EOT
  value = [
    for name in sort(keys(var.demo_accounts)) :
    "aws cognito-idp admin-set-user-password --region ${var.region} --user-pool-id ${aws_cognito_user_pool.demo.id} --username ${name} --password '<choose one>' --permanent"
  ]
}
