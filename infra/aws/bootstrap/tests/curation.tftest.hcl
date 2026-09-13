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
run "curation_roles_have_specific_boundaries" {
  command = plan
  assert {
    condition = (
      local.role_boundary_groups.CurationConsumerLambda.role_names == ["slice-test-curation-consumer-lambda"] &&
      local.role_boundary_groups.CurationConsumerLambda.boundary == aws_iam_policy.curation_consumer_lambda_boundary.arn &&
      local.role_boundary_groups.CurationOutboxRelayLambda.role_names == ["slice-test-curation-outbox-relay-lambda"] &&
      local.role_boundary_groups.CurationOutboxRelayLambda.boundary == aws_iam_policy.curation_outbox_relay_lambda_boundary.arn &&
      local.role_boundary_groups.CurationOutboxRelayScheduler.role_names == ["slice-test-curation-outbox-relay-scheduler"] &&
      local.role_boundary_groups.CurationOutboxRelayScheduler.boundary == aws_iam_policy.curation_outbox_relay_scheduler_boundary.arn &&
      alltrue([for policy in [
        aws_iam_policy.curation_consumer_lambda_boundary.policy,
        aws_iam_policy.curation_outbox_relay_lambda_boundary.policy,
        aws_iam_policy.curation_outbox_relay_scheduler_boundary.policy,
      ] : contains(jsondecode(policy).Statement, local.boundary_no_escalation_statement)])
    )
    error_message = "Curationの3ロールと権限境界を対応させ、権限昇格を禁止する。"
  }
  assert {
    condition = (
      { for s in jsondecode(aws_iam_policy.curation_consumer_lambda_boundary.policy).Statement : s.Sid => s.Resource if contains(["ConsumeCurationEvents", "ReadGeminiKey", "RdsIamAuthAsApp"], s.Sid) } == {
        ConsumeCurationEvents = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation"
        ReadGeminiKey         = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/curation-consumer/gemini-api-key"
        RdsIamAuthAsApp       = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_app"
      } &&
      [for s in jsondecode(aws_iam_policy.curation_outbox_relay_lambda_boundary.policy).Statement : s.Resource if s.Sid == "SendPipelineEvents"] == ["arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation"] &&
      [for s in jsondecode(aws_iam_policy.curation_outbox_relay_scheduler_boundary.policy).Statement : s.Resource if s.Sid == "InvokeRelayOnly"] == [local.curation_outbox_relay_lambda_arn]
    )
    error_message = "Consumer・relay・Schedulerの天井をそれぞれの対象だけに限定する。"
  }
}

run "ci_limits_curation_management_to_its_function_and_tag" {
  command = plan
  assert {
    condition = (
      aws_iam_role_policy_attachment.apply_curation_consumer.role == aws_iam_role.ci["apply"].name &&
      aws_iam_role_policy_attachment.apply_curation_consumer.policy_arn == aws_iam_policy.apply_curation_consumer.arn &&
      contains(local.managed_role_arns, local.curation_consumer_role_arn) &&
      contains(local.managed_role_arns, local.curation_outbox_relay_role_arn) &&
      !contains(local.app_role_arns, local.curation_consumer_role_arn) &&
      !contains(local.app_role_arns, local.curation_outbox_relay_role_arn) &&
      [for s in jsondecode(aws_iam_policy.apply_curation_consumer.policy).Statement : s.Resource if s.Sid == "ManageCurationFunction"] == [local.curation_consumer_lambda_arn] &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_curation_consumer.policy).Statement :
        !contains(["CreateCurationMapping", "ManageCurationMapping"], s.Sid) ? true :
        s.Condition.ArnEquals["lambda:FunctionArn"] == local.curation_consumer_lambda_arn &&
        values(s.Condition.StringEquals) == ["slice-test-curation-consumer"]
      ]) &&
      alltrue([for policy in [aws_iam_policy.curation_consumer_lambda_boundary.policy, aws_iam_policy.curation_outbox_relay_lambda_boundary.policy] :
        [for s in jsondecode(policy).Statement : s.Condition.ArnEquals["lambda:SourceFunctionArn"] if contains(["DenyEniOperationsFromFunctionCode", "DenyNetworkManagementFromFunctionCode"], s.Sid)] == [
          policy == aws_iam_policy.curation_consumer_lambda_boundary.policy ? local.curation_consumer_lambda_arn : local.curation_outbox_relay_lambda_arn
        ]
      ])
    )
    error_message = "Curationの管理対象を関数・タグで限定し、関数コードからのENI操作を拒否する。"
  }
}
