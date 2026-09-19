mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_kms_key" { defaults = { arn = "arn:aws:kms:ap-northeast-1:123456789012:key/test" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_s3_bucket" { defaults = { arn = "arn:aws:s3:::test" } }
  mock_resource "aws_iam_openid_connect_provider" { defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" } }
}

variables {
  name_prefix  = "slice-test"
  github_owner = "test-owner"
  github_repo  = "test-repo"
  root_domain  = "example.com"
}

run "cleanup_roles_have_dedicated_boundaries_and_ci_scope" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.auth_rate_limit_cleanup_lambda_boundary.policy).Statement[0].Resource == "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_auth_rate_limit_cleanup" &&
      local.role_boundary_groups.AuthRateLimitCleanupLambda.role_names == ["slice-test-auth-rate-limit-cleanup-lambda"] &&
      local.role_boundary_groups.AuthRateLimitCleanupScheduler.role_names == ["slice-test-auth-rate-limit-cleanup-scheduler"] &&
      contains(local.lambda_config_function_arns, local.auth_rate_limit_cleanup_lambda_arn) &&
      aws_iam_role_policy_attachment.apply_auth_rate_limit_cleanup.role == aws_iam_role.ci["apply"].name
    )
    error_message = "専用ロール・boundary・設定読取・apply許可を掃除Lambdaだけへ追加する。"
  }
}

run "cleanup_roles_preserve_pass_role_pairing" {
  command = plan
  assert {
    condition = (
      contains(local.outbox_service_roles.Lambda.arns, local.auth_rate_limit_cleanup_lambda_role_arn) &&
      !contains(local.outbox_service_roles.Scheduler.arns, local.auth_rate_limit_cleanup_lambda_role_arn) &&
      contains(local.outbox_service_roles.Scheduler.arns, local.auth_rate_limit_cleanup_scheduler_role_arn) &&
      !contains(local.outbox_service_roles.Lambda.arns, local.auth_rate_limit_cleanup_scheduler_role_arn)
    )
    error_message = "LambdaとSchedulerのPassRole先を対応するサービスへ固定する。"
  }
}
