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


run "ci_limits_curation_management_to_its_function_and_tag" {
  command = plan
  assert {
    condition = (
      aws_iam_role_policy_attachment.apply_curation_consumer.role == aws_iam_role.ci["apply"].name &&
      aws_iam_role_policy_attachment.apply_curation_consumer.policy_arn == aws_iam_policy.apply_curation_consumer.arn &&
      [for s in jsondecode(aws_iam_policy.apply_curation_consumer.policy).Statement : s.Resource if s.Sid == "ManageCurationFunction"] == [local.curation_consumer_lambda_arn] &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_curation_consumer.policy).Statement :
        !contains(["CreateCurationMapping", "ManageCurationMapping"], s.Sid) ? true :
        s.Condition.ArnEquals["lambda:FunctionArn"] == local.curation_consumer_lambda_arn &&
        values(s.Condition.StringEquals) == ["slice-test-curation-consumer"]
      ])
    )
    error_message = "Curationの管理対象を関数・タグで限定する。"
  }
}

