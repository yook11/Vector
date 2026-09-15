mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:ap-northeast-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/test-policy" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-role" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::test-state" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}

variables {
  name_prefix  = "slice-test"
  github_owner = "test-owner"
  github_repo  = "test-repo"
  root_domain  = "example.com"
}


run "dispatch_permissions_remain_bounded" {
  command = plan
  assert {
    condition = (
      local.role_boundary_groups["SourceDispatchLambda"].boundary == aws_iam_policy.source_dispatch_lambda_boundary.arn &&
      local.role_boundary_groups["SourceDispatchScheduler"].boundary == aws_iam_policy.source_dispatch_scheduler_boundary.arn &&
      contains(local.outbox_service_roles.Lambda.arns, local.source_dispatch_lambda_role_arn) &&
      contains(local.outbox_service_roles.Scheduler.arns, local.source_dispatch_scheduler_role_arn) &&
      contains(local.lambda_config_function_arns, local.source_dispatch_lambda_arn) &&
      alltrue([for arn in values(local.source_dispatch_queue_arns) : contains(local.managed_pipeline_queue_arns, arn)])
    )
    error_message = "配備・PassRole・設定復号の対象と専用境界を接続する。"
  }
  assert {
    condition = (
      jsondecode(aws_iam_policy.source_dispatch_lambda_boundary.policy).Statement[0].Resource == "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_collect" &&
      toset(jsondecode(aws_iam_policy.source_dispatch_lambda_boundary.policy).Statement[1].Resource) == toset([local.source_dispatch_queue_arns["acquisition"], local.source_dispatch_queue_arns["execution_failure"]]) &&
      jsondecode(aws_iam_policy.source_dispatch_scheduler_boundary.policy).Statement[0].Resource == local.source_dispatch_lambda_arn &&
      jsondecode(aws_iam_policy.source_dispatch_scheduler_boundary.policy).Statement[1].Resource == local.source_dispatch_queue_arns["scheduler_failure"] &&
      jsondecode(aws_iam_policy.apply_source_dispatch.policy).Statement[0].Resource == [local.source_dispatch_lambda_arn] &&
      toset(jsondecode(aws_iam_policy.apply_source_dispatch.policy).Statement[1].Resource) == toset([for cadence in ["high", "medium", "low"] : "arn:aws:scheduler:ap-northeast-1:123456789012:schedule/slice-test-source-dispatch/slice-test-source-dispatch-${cadence}"]) &&
      length(local.source_dispatch_boundary_pairing_statements) == 2
    )
    error_message = "無関係なDBユーザー・キュー・Lambda・スケジュールを許可しない。"
  }
  assert {
    condition     = alltrue([for policy in [aws_iam_policy.source_dispatch_lambda_boundary.policy, aws_iam_policy.source_dispatch_scheduler_boundary.policy, aws_iam_policy.apply_source_dispatch.policy, aws_iam_policy.apply_pass_role.policy, aws_iam_policy.lambda_config_readback.policy, aws_iam_policy.apply_role_creation.policy] : length(policy) <= 6144]) && length(aws_iam_role_policy.apply.policy) <= 10240
    error_message = "新規ロールを追加してもIAMポリシー容量上限を超えない。"
  }
}
