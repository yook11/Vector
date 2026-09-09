mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
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

override_resource {
  override_during = plan
  target          = aws_iam_policy.embedding_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-embedding-consumer-lambda-boundary" }
}

run "consumer_boundary_has_only_required_permissions" {
  command = plan

  assert {
    condition = (
      length(jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement) == 7 &&
      toset([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement : s.Sid]) == toset(["ConsumeEmbeddingEvents", "ReadGeminiKey", "RdsIamAuthAsApp", "WriteConsumerLogs", "ManageLambdaNetworkInterfaces", "DenyEniOperationsFromFunctionCode", "NoPrivilegeEscalation"]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Effect == (contains(["DenyEniOperationsFromFunctionCode", "NoPrivilegeEscalation"], s.Sid) ? "Deny" : "Allow")
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        !contains(["ManageLambdaNetworkInterfaces", "DenyEniOperationsFromFunctionCode"], s.Sid) ? true :
        s.Resource == "*" && toset(s.Action) == toset([
          "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
          "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
        ])
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "ConsumeEmbeddingEvents" ? true :
        s.Effect == "Allow" && toset(s.Action) == toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]) &&
        s.Resource == "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "ReadGeminiKey" ? true :
        s.Action == "ssm:GetParameter" && s.Resource == "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/embedding-consumer/gemini-api-key"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "RdsIamAuthAsApp" ? true :
        s.Action == "rds-db:connect" && s.Resource == "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_app"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "WriteConsumerLogs" ? true :
        toset(s.Action) == toset(["logs:CreateLogStream", "logs:PutLogEvents"]) &&
        s.Resource == "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-embedding-consumer:*"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "DenyEniOperationsFromFunctionCode" ? true :
        s.Effect == "Deny" && toset(s.Action) == toset(local.embedding_consumer_eni_actions) &&
        s.Condition.ArnEquals["lambda:SourceFunctionArn"] == local.embedding_consumer_lambda_arn
      ])
    )
    error_message = "Consumerのboundaryは対象の受信・DB・SSM・ログ・ENIに限定する。"
  }
  assert {
    condition = (
      local.role_boundary_groups.EmbeddingConsumerLambda.role_names == ["slice-test-embedding-consumer-lambda"] &&
      local.role_boundary_groups.EmbeddingConsumerLambda.boundary == aws_iam_policy.embedding_consumer_lambda_boundary.arn &&
      contains(local.managed_role_arns, local.embedding_consumer_role_arn) &&
      !contains(local.app_role_arns, local.embedding_consumer_role_arn) &&
      alltrue([for s in local.boundary_pairing_statements : s.Sid != "DenyWideBoundaryOnEmbeddingConsumerLambdaRoles" ? true :
        s.Resource == [local.embedding_consumer_role_arn] &&
        s.Condition.StringNotEquals["iam:PermissionsBoundary"] == aws_iam_policy.embedding_consumer_lambda_boundary.arn
      ])
    )
    error_message = "Consumerロールの作成を専用boundaryに拘束し、ECS反映対象へ混ぜない。"
  }
}

run "ci_can_manage_dlq_without_granting_relay_send" {
  command = plan

  assert {
    condition = (
      length(aws_iam_role_policy.apply.policy) <= 10240 &&
      length(aws_iam_policy.apply_outbox.policy) <= 6144 &&
      length(aws_iam_policy.embedding_consumer_lambda_boundary.policy) <= 6144
    )
    error_message = "ロール追加後もIAM policyのサイズ上限内に収める。"
  }

  assert {
    condition = (
      toset(local.managed_pipeline_queue_arns) == setunion(toset(local.outbox_queue_arns), toset([local.embedding_dlq_arn])) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_outbox.policy).Statement : s.Sid != "ManagePipelineQueues" ? true :
        toset(s.Resource) == toset(local.managed_pipeline_queue_arns) &&
        !contains(s.Action, "sqs:SendMessage") && !contains(s.Action, "sqs:ReceiveMessage") && !contains(s.Action, "sqs:PurgeQueue")
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.outbox_relay_lambda_boundary.policy).Statement : s.Sid != "SendPipelineEvents" ? true :
        toset(s.Resource) == toset(local.outbox_queue_arns) && !contains(s.Resource, local.embedding_dlq_arn)
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_outbox.policy).Statement : s.Sid != "ManageOutboxLambda" ? true :
        s.Resource == local.outbox_lambda_arn
      ])
    )
    error_message = "CIだけにDLQ管理を追加し、relay送信先と関数管理対象は維持する。"
  }
}

run "passrole_allows_only_pipeline_lambda_roles" {
  command = plan

  assert {
    condition = (
      length(local.outbox_pass_role_guards) == 4 &&
      toset([for s in local.outbox_pass_role_guards : s.Sid]) == toset([
        "DenyPassRoleToLambdaExceptPipelineRoles", "DenyPipelineLambdaRolesToOtherServices",
        "DenyPassRoleToSchedulerExceptPipelineRoles", "DenyPipelineSchedulerRolesToOtherServices",
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPassRoleToLambdaExceptPipelineRoles" ? true :
        s.Effect == "Deny" && s.Action == "iam:PassRole" &&
        toset(s.NotResource) == toset([
          "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda",
          local.embedding_consumer_role_arn,
        ]) && s.Condition.StringEquals["iam:PassedToService"] == "lambda.amazonaws.com"
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPipelineLambdaRolesToOtherServices" ? true :
        s.Effect == "Deny" && s.Action == "iam:PassRole" &&
        toset(s.Resource) == toset([
          "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda",
          local.embedding_consumer_role_arn,
        ]) && s.Condition.StringNotEquals["iam:PassedToService"] == "lambda.amazonaws.com"
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPassRoleToSchedulerExceptPipelineRoles" ? true :
        s.NotResource == ["arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler"] &&
        s.Condition.StringEquals["iam:PassedToService"] == "scheduler.amazonaws.com"
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPipelineSchedulerRolesToOtherServices" ? true :
        s.Resource == ["arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler"] &&
        s.Effect == "Deny" && s.Condition.StringNotEquals["iam:PassedToService"] == "scheduler.amazonaws.com"
      ]) &&
      alltrue([for guard in local.outbox_pass_role_guards : contains(jsondecode(aws_iam_policy.apply_outbox.policy).Statement, guard)])
    )
    error_message = "LambdaとSchedulerのPassRole制約を双方向に維持する。"
  }
}
