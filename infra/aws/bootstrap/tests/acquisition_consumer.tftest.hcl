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



run "acquisition_permissions_are_bounded" {
  command = plan
  assert {
    condition = (
      local.role_boundary_groups["AcquisitionConsumerLambda"].boundary == aws_iam_policy.acquisition_consumer_lambda_boundary.arn &&
      contains(local.outbox_service_roles.Lambda.arns, local.acquisition_consumer_role_arn) &&
      contains(local.lambda_config_function_arns, local.acquisition_consumer_lambda_arn) &&
      contains(local.managed_pipeline_queue_arns, local.acquisition_dlq_arn) &&
      jsondecode(aws_iam_policy.acquisition_consumer_lambda_boundary.policy).Statement[0].Resource == local.source_dispatch_queue_arns["acquisition"] &&
      toset(jsondecode(aws_iam_policy.acquisition_consumer_lambda_boundary.policy).Statement[0].Action) == toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]) &&
      endswith(jsondecode(aws_iam_policy.acquisition_consumer_lambda_boundary.policy).Statement[1].Resource, "/vector_collect") &&
      jsondecode(aws_iam_policy.apply_acquisition_consumer.policy).Statement[0].Resource == local.acquisition_consumer_lambda_arn &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_acquisition_consumer.policy).Statement :
        !contains(["CreateAcquisitionMapping", "ManageAcquisitionMapping"], s.Sid) ? true : s.Condition.ArnEquals["lambda:FunctionArn"] == local.acquisition_consumer_lambda_arn
      ]) &&
      length(aws_iam_policy.apply_acquisition_consumer.policy) <= 6144 &&
      length(aws_iam_policy.apply_pass_role.policy) <= 6144 &&
      length(aws_iam_role_policy.apply.policy) <= 10240
    )
    error_message = "専用境界・管理対象・PassRoleを限定し、IAM容量上限を守る。"
  }
}
