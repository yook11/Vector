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


# 専用ロールは専用boundaryに固定し、既存の権限昇格拒否を維持する。
run "completion_roles_have_specific_boundaries" {
  command = plan
  assert {
    condition = (
      local.role_boundary_groups.CompletionConsumerLambda.role_names == ["slice-test-completion-consumer-lambda"] &&
      local.role_boundary_groups.CompletionConsumerLambda.boundary == aws_iam_policy.completion_consumer_lambda_boundary.arn &&
      local.role_boundary_groups.CompletionOutboxRelayLambda.role_names == ["slice-test-completion-outbox-relay-lambda"] &&
      local.role_boundary_groups.CompletionOutboxRelayLambda.boundary == aws_iam_policy.completion_outbox_relay_lambda_boundary.arn &&
      local.role_boundary_groups.CompletionOutboxRelayScheduler.role_names == ["slice-test-completion-outbox-relay-scheduler"] &&
      local.role_boundary_groups.CompletionOutboxRelayScheduler.boundary == aws_iam_policy.completion_outbox_relay_scheduler_boundary.arn &&
      alltrue([for policy in [
        aws_iam_policy.completion_consumer_lambda_boundary.policy,
        aws_iam_policy.completion_outbox_relay_lambda_boundary.policy,
        aws_iam_policy.completion_outbox_relay_scheduler_boundary.policy,
      ] : contains(jsondecode(policy).Statement, local.boundary_no_escalation_statement)])
    )
    error_message = "Completionの3ロールと権限境界を対応させ、権限昇格を禁止する。"
  }
  assert {
    condition = (
      { for s in jsondecode(aws_iam_policy.completion_consumer_lambda_boundary.policy).Statement : s.Sid => s.Resource if contains(["ConsumeCompletionEvents", "RdsIamAuthAsCollect"], s.Sid) } == {
        ConsumeCompletionEvents = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion"
        RdsIamAuthAsCollect     = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_collect"
      } &&
      [for s in jsondecode(aws_iam_policy.completion_outbox_relay_lambda_boundary.policy).Statement : s.Resource if s.Sid == "SendPipelineEvents"] == ["arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion"] &&
      [for s in jsondecode(aws_iam_policy.completion_outbox_relay_scheduler_boundary.policy).Statement : s.Resource if s.Sid == "InvokeRelayOnly"] == [local.completion_outbox_relay_lambda_arn]
    )
    error_message = "Consumer・relay・Schedulerの天井をそれぞれの対象だけに限定する。"
  }
}

run "ci_limits_completion_management_to_its_function_and_tag" {
  command = plan
  assert {
    condition = (
      aws_iam_role_policy_attachment.apply_completion_consumer.role == aws_iam_role.ci["apply"].name &&
      aws_iam_role_policy_attachment.apply_completion_consumer.policy_arn == aws_iam_policy.apply_completion_consumer.arn &&
      contains(local.managed_role_arns, local.completion_consumer_role_arn) &&
      contains(local.managed_role_arns, local.completion_outbox_relay_role_arn) &&
      !contains(local.app_role_arns, local.completion_consumer_role_arn) &&
      !contains(local.app_role_arns, local.completion_outbox_relay_role_arn) &&
      [for s in jsondecode(aws_iam_policy.apply_completion_consumer.policy).Statement : s.Resource if s.Sid == "ManageCompletionFunction"] == [local.completion_consumer_lambda_arn] &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_completion_consumer.policy).Statement :
        !contains(["CreateCompletionMapping", "ManageCompletionMapping"], s.Sid) ? true :
        s.Condition.ArnEquals["lambda:FunctionArn"] == local.completion_consumer_lambda_arn &&
        values(s.Condition.StringEquals) == ["slice-test-completion-consumer"]
      ]) &&
      alltrue([for policy in [aws_iam_policy.completion_consumer_lambda_boundary.policy, aws_iam_policy.completion_outbox_relay_lambda_boundary.policy] :
        [for s in jsondecode(policy).Statement : s.Condition.ArnEquals["lambda:SourceFunctionArn"] if contains(["DenyEniOperationsFromFunctionCode", "DenyNetworkManagementFromFunctionCode"], s.Sid)] == [
          policy == aws_iam_policy.completion_consumer_lambda_boundary.policy ? local.completion_consumer_lambda_arn : local.completion_outbox_relay_lambda_arn
        ]
      ])
    )
    error_message = "Completionの管理対象を関数・タグで限定し、関数コードからのENI操作を拒否する。"
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

run "relay_boundary_allows_only_dedicated_db_user" {
  command = plan
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_policy.completion_outbox_relay_lambda_boundary.policy).Statement : s.Resource
      if s.Action == "rds-db:connect" && s.Effect == "Allow"
      ])) == toset([
      "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_outbox_relay",
    ])
    error_message = "Completion Relayの境界は専用DBユーザーだけに接続を許可し、旧Appユーザーへの接続を許可しない。"
  }
}
