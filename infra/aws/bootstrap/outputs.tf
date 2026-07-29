output "state_bucket_name" {
  description = "本体スタックの backend \"s3\" に書く bucket 名。"
  value       = aws_s3_bucket.tfstate.id
}

output "permissions_boundary_arn" {
  description = <<-EOT
    本体スタックが作る全ロールに付ける boundary。
    本体側は data source ではなく variable で受け取る
    (apply に /vector-ci/ への iam:Get* を要求しないため)。
  EOT
  value       = aws_iam_policy.boundary.arn
}

output "ci_role_arns" {
  description = "GitHub Actions の aws-actions/configure-aws-credentials に渡す。"
  value       = { for name, role in aws_iam_role.ci : name => role.arn }
}

output "github_oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.github.arn
}

output "hosted_zone_name_servers" {
  description = <<-EOT
    レジストラの NS レコードに設定する 4 本。
    委任が完了するまで ACM の DNS 検証は通らない。
  EOT
  value       = aws_route53_zone.this.name_servers
}
