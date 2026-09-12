output "state_bucket_name" { value = aws_s3_bucket.state.id }
output "terraform_role_arn" { value = aws_iam_role.terraform.arn }
output "repositories" { value = { for key, repo in aws_ecr_repository.images : key => repo.repository_url } }
output "runtime_boundaries" { value = { for key, policy in aws_iam_policy.runtime_boundary : key => policy.arn } }
output "gemini_parameter_path" {
  description = "Standard SecureStringとして別途登録するパス；秘密値はTerraformで扱わない。"
  value       = local.parameter
}
