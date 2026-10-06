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


run "ci_limits_completion_management_to_its_function_and_tag" {
  command = plan
  assert {
    condition = (
      aws_iam_role_policy_attachment.apply_completion_consumer.role == aws_iam_role.ci["apply"].name &&
      aws_iam_role_policy_attachment.apply_completion_consumer.policy_arn == aws_iam_policy.apply_completion_consumer.arn &&
      [for s in jsondecode(aws_iam_policy.apply_completion_consumer.policy).Statement : s.Resource if s.Sid == "ManageCompletionFunction"] == [local.completion_consumer_lambda_arn] &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_completion_consumer.policy).Statement :
        !contains(["CreateCompletionMapping", "ManageCompletionMapping"], s.Sid) ? true :
        s.Condition.ArnEquals["lambda:FunctionArn"] == local.completion_consumer_lambda_arn &&
        values(s.Condition.StringEquals) == ["slice-test-completion-consumer"]
      ])
    )
    error_message = "Completionの管理対象を関数・タグで限定する。"
  }
}

run "pass_role_guards_survive_policy_relocation" {
  command = plan
  assert {
    condition = (
      length(aws_iam_policy.apply_pass_role.policy) <= 6144 &&
      aws_iam_role_policy_attachment.apply_pass_role.role == aws_iam_role.ci["apply"].name &&
      alltrue([for guard in local.outbox_pass_role_guards : contains(jsondecode(aws_iam_policy.apply_pass_role.policy).Statement, guard)]) &&
      [for s in jsondecode(aws_iam_policy.apply_pass_role.policy).Statement : s.Condition.StringNotEquals["iam:PassedToService"] if s.Sid == "DenyPassRoleToUnintendedServices"] == [["ecs-tasks.amazonaws.com", "chatbot.amazonaws.com", "bedrock-agentcore.amazonaws.com", "lambda.amazonaws.com", "scheduler.amazonaws.com"]] &&
      [for s in jsondecode(aws_iam_policy.apply_pass_role.policy).Statement : s.NotResource if s.Sid == "DenyPassRoleToAgentCoreExceptGateway"] == ["arn:aws:iam::123456789012:role/slice-test/slice-test-agentcore-gateway"] &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_pass_role.policy).Statement : s.Effect == "Deny" && s.Action == "iam:PassRole"])
    )
    error_message = "ポリシー移設後もサービスとロールのPassRole拒否を維持し、容量上限を守る。"
  }
}

