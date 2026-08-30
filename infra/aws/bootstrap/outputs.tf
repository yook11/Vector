output "state_bucket_name" {
  description = "本体スタックの backend \"s3\" に書く bucket 名。"
  value       = aws_s3_bucket.tfstate.id
}

output "role_boundaries" {
  description = <<-EOT
    ロール名 -> その天井。本体スタックは ARN を名前から組み立てるので
    (iam.tf の boundary_arns)、この output は値を渡すためのものではなく、
    apply 後に AWS の実態 (iam get-role の PermissionsBoundary) と
    突き合わせるための期待値として使う。
  EOT
  value = {
    for key, group in local.role_boundary_groups :
    key => { boundary = group.boundary, role_names = group.role_names }
  }
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
