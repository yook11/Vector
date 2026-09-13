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
run "assessment_roles_have_specific_boundaries" {
  command = plan
  assert {
    condition = (
      local.role_boundary_groups.AssessmentConsumerLambda.role_names == ["slice-test-assessment-consumer-lambda"] &&
      local.role_boundary_groups.AssessmentConsumerLambda.boundary == aws_iam_policy.assessment_consumer_lambda_boundary.arn &&
      local.role_boundary_groups.AssessmentOutboxRelayLambda.role_names == ["slice-test-assessment-outbox-relay-lambda"] &&
      local.role_boundary_groups.AssessmentOutboxRelayLambda.boundary == aws_iam_policy.assessment_outbox_relay_lambda_boundary.arn &&
      local.role_boundary_groups.AssessmentOutboxRelayScheduler.role_names == ["slice-test-assessment-outbox-relay-scheduler"] &&
      local.role_boundary_groups.AssessmentOutboxRelayScheduler.boundary == aws_iam_policy.assessment_outbox_relay_scheduler_boundary.arn &&
      alltrue([for policy in [
        aws_iam_policy.assessment_consumer_lambda_boundary.policy,
        aws_iam_policy.assessment_outbox_relay_lambda_boundary.policy,
        aws_iam_policy.assessment_outbox_relay_scheduler_boundary.policy,
      ] : contains(jsondecode(policy).Statement, local.boundary_no_escalation_statement)])
    )
    error_message = "Assessmentの3ロールと権限境界を対応させ、権限昇格を禁止する。"
  }
  assert {
    condition = (
      { for s in jsondecode(aws_iam_policy.assessment_consumer_lambda_boundary.policy).Statement : s.Sid => s.Resource if contains(["ConsumeAssessmentEvents", "ReadDeepSeekKey", "RdsIamAuthAsApp"], s.Sid) } == {
        ConsumeAssessmentEvents = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment"
        ReadDeepSeekKey         = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/assessment-consumer/deepseek-api-key"
        RdsIamAuthAsApp         = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_app"
      } &&
      [for s in jsondecode(aws_iam_policy.assessment_outbox_relay_lambda_boundary.policy).Statement : s.Resource if s.Sid == "SendPipelineEvents"] == ["arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment"] &&
      [for s in jsondecode(aws_iam_policy.assessment_outbox_relay_scheduler_boundary.policy).Statement : s.Resource if s.Sid == "InvokeRelayOnly"] == [local.assessment_outbox_relay_lambda_arn]
    )
    error_message = "Consumer・relay・Schedulerの天井をそれぞれの対象だけに限定する。"
  }
}

# CIの許可一覧と容量を検証し、追加した資源を通常の適用経路で管理できるようにする。
run "ci_manages_assessment_without_broadening_mapping_access" {
  command = plan
  assert {
    condition = (
      length(aws_iam_role_policy.apply.policy) <= 10240 &&
      alltrue([for policy in [
        aws_iam_policy.apply_outbox.policy,
        aws_iam_policy.apply_assessment_consumer.policy,
        aws_iam_policy.apply_embedding_consumer.policy,
        aws_iam_policy.assessment_consumer_lambda_boundary.policy,
        aws_iam_policy.assessment_outbox_relay_lambda_boundary.policy,
        aws_iam_policy.assessment_outbox_relay_scheduler_boundary.policy,
        aws_iam_policy.lambda_config_readback.policy,
        aws_iam_policy.apply_curation_consumer.policy,
        aws_iam_policy.curation_consumer_lambda_boundary.policy,
        aws_iam_policy.curation_outbox_relay_lambda_boundary.policy,
        aws_iam_policy.curation_outbox_relay_scheduler_boundary.policy,
      ] : length(policy) <= 6144]) &&
      aws_iam_role_policy_attachment.apply_assessment_consumer.role == aws_iam_role.ci["apply"].name &&
      contains(local.managed_pipeline_queue_arns, local.assessment_dlq_arn) &&
      contains(local.managed_role_arns, local.assessment_consumer_role_arn) &&
      contains(local.managed_role_arns, local.assessment_outbox_relay_role_arn) &&
      !contains(local.app_role_arns, local.assessment_consumer_role_arn)
    )
    error_message = "IAM policy容量: inline=${length(aws_iam_role_policy.apply.policy)}, outbox=${length(aws_iam_policy.apply_outbox.policy)}。許可一覧とrollout分離も維持する。"
  }
  assert {
    condition = (
      [for s in jsondecode(aws_iam_policy.apply_assessment_consumer.policy).Statement : s.Resource if s.Sid == "ManageAssessmentFunction"] == [local.assessment_consumer_lambda_arn] &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_assessment_consumer.policy).Statement :
        !contains(["CreateAssessmentMapping", "ManageAssessmentMapping"], s.Sid) ? true :
        s.Condition.ArnEquals["lambda:FunctionArn"] == local.assessment_consumer_lambda_arn &&
        values(s.Condition.StringEquals) == ["slice-test-assessment-consumer"]
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_outbox.policy).Statement :
        s.Sid != "ManageOutboxLambda" ? true : toset(s.Resource) == toset([local.outbox_lambda_arn, local.assessment_outbox_relay_lambda_arn, local.curation_outbox_relay_lambda_arn])
      ])
    )
    error_message = "関数のARNとmappingのタグ条件を固定し、他Consumerを管理しない。"
  }
}

# 拒否条件をpolicy間で移しても、各ロールのboundary固定を欠落・重複させない。
run "boundary_pairing_guards_remain_complete" {
  command = plan
  assert {
    condition = alltrue([for expected in local.boundary_pairing_statements :
      [for actual in concat(
        jsondecode(aws_iam_role_policy.apply.policy).Statement,
        jsondecode(aws_iam_policy.apply_outbox.policy).Statement,
        jsondecode(aws_iam_policy.apply_curation_consumer.policy).Statement,
        jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement,
        jsondecode(aws_iam_policy.apply_assessment_consumer.policy).Statement
      ) : jsonencode(actual) if actual.Sid == expected.Sid] == [jsonencode(expected)]
    ])
    error_message = "既存・追加の全ロールで、同じboundary固定Denyを1件ずつ保持する。"
  }
}

# ARNの実際の長さを使い、ポリシー容量の過小評価を防ぐ。
override_resource {
  override_during = plan
  target          = aws_iam_policy.task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.agent_task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-agent-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.migration_task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-migration-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.execution_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-execution-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.migration_execution_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-migration-execution-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.embedding_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-embedding-consumer-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-outbox-relay-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.assessment_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-assessment-consumer-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.assessment_outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-assessment-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.assessment_outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-assessment-outbox-relay-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.chatbot_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-chatbot-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.agentcore_gateway_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-agentcore-gateway-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.curation_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-curation-consumer-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.curation_outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-curation-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.curation_outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-curation-outbox-relay-scheduler-boundary" }
}
