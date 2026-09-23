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

# 許可表の容量を実際のARN長で測るため、全boundaryのARNを本番と同じ形にする。
override_resource {
  override_during = plan
  target          = aws_iam_policy.acquisition_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-acquisition-consumer-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.agent_task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-agent-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.agentcore_gateway_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-agentcore-gateway-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.article_analysis_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-article-analysis-lambda-boundary" }
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
  target          = aws_iam_policy.auth_rate_limit_cleanup_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-auth-rate-limit-cleanup-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.auth_rate_limit_cleanup_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-auth-rate-limit-cleanup-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.backfill_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-backfill-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.backfill_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-backfill-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.chatbot_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-chatbot-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.completion_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-completion-consumer-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.completion_outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-completion-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.completion_outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-completion-outbox-relay-scheduler-boundary" }
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
  target          = aws_iam_policy.migration_task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-migration-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-outbox-relay-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.source_dispatch_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-source-dispatch-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.source_dispatch_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-source-dispatch-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-task-boundary" }
}

run "shared_role_requires_article_analysis_boundary" {
  command = plan
  assert {
    condition = (
      local.role_boundary_groups["ArticleAnalysisLambda"].boundary == aws_iam_policy.article_analysis_lambda_boundary.arn &&
      local.role_boundary_groups["ArticleAnalysisLambda"].role_names == ["slice-test-article-analysis-lambda"] &&
      contains(local.managed_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-article-analysis-lambda") &&
      !contains(local.app_role_arns, local.article_analysis_lambda_role_arn) &&
      local.boundary_pairing_statements_by_group["ArticleAnalysisLambda"].Resource == [local.article_analysis_lambda_role_arn] &&
      local.boundary_pairing_statements_by_group["ArticleAnalysisLambda"].Condition.StringNotEquals["iam:PermissionsBoundary"] == aws_iam_policy.article_analysis_lambda_boundary.arn &&
      contains(jsondecode(aws_iam_role_policy.apply.policy).Statement, local.boundary_pairing_statements_by_group["ArticleAnalysisLambda"])
    )
    error_message = "AI分析の共通ロールを専用boundaryに固定し、CIの作成制約を取り付ける。"
  }
}

run "boundary_limits_analysis_to_its_queues_keys_logs_and_database_user" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.article_analysis_lambda_boundary.policy).Statement == [
        { Sid = "ConsumeAnalysisEvents", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = [for stage in ["curation", "assessment", "embedding"] : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-${stage}"] },
        { Sid = "RdsIamAuthAsArticleAnalysis", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_article_analysis" },
        { Sid = "ReadAiProviderKeys", Effect = "Allow", Action = "ssm:GetParameter", Resource = [
          "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/curation-consumer/gemini-api-key",
          "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/assessment-consumer/deepseek-api-key",
          "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/embedding-consumer/gemini-api-key",
        ] },
        { Sid = "ReadFrontendNotificationKey", Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/frontend/revalidate-bearer-secret" },
        { Sid = "WriteConsumerLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = [for stage in ["curation", "assessment", "embedding"] : "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-${stage}-consumer:*"] },
        { Sid = "ManageLambdaNetworkInterfaces", Effect = "Allow", Action = local.outbox_lambda_eni_actions, Resource = "*" },
        { Sid = "DenyEniOperationsFromFunctionCode", Effect = "Deny", Action = local.outbox_lambda_eni_actions, Resource = "*", Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [for stage in ["curation", "assessment", "embedding"] : "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-${stage}-consumer"] } } },
        local.boundary_no_escalation_statement,
      ]
    )
    error_message = "天井をAI分析の3キュー・3つのAIキーと通知キー・3ロググループに限定し、ENIのコード実行と権限昇格を拒否する。"
  }
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_policy.article_analysis_lambda_boundary.policy).Statement : s.Resource
      if s.Action == "rds-db:connect" && s.Effect == "Allow"
    ])) == toset(["arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_article_analysis"])
    error_message = "AI分析の天井は記事分析専用のDBユーザーだけに接続を許可し、vector_appへの接続を許可しない。"
  }
}

run "pass_role_admits_only_shared_analysis_role" {
  command = plan
  assert {
    condition = (
      contains(local.outbox_service_roles.Lambda.arns, local.article_analysis_lambda_role_arn) &&
      !contains(local.outbox_service_roles.Scheduler.arns, local.article_analysis_lambda_role_arn) &&
      alltrue([for stage in ["curation", "assessment", "embedding"] :
        !contains(local.outbox_service_roles.Lambda.arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-${stage}-consumer-lambda") &&
        !contains(local.managed_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-${stage}-consumer-lambda")
      ]) &&
      length(setintersection(toset(keys(local.role_boundary_groups)), toset(["CurationConsumerLambda", "AssessmentConsumerLambda", "EmbeddingConsumerLambda"]))) == 0 &&
      alltrue([for guard in local.outbox_pass_role_guards : contains(jsondecode(aws_iam_policy.apply_pass_role.policy).Statement, guard)])
    )
    error_message = "共通ロールはLambdaにだけ渡せるようにし、関数ごとの旧3ロールはPassRole・ロール作成の許可表と対応表から除く。"
  }
}

run "policies_stay_within_iam_size_limits" {
  command = plan
  assert {
    condition = (
      length(aws_iam_policy.apply_role_creation.policy) <= 6144 &&
      length(aws_iam_policy.apply_pass_role.policy) <= 6144 &&
      length(aws_iam_role_policy.apply.policy) <= 10240 &&
      length(aws_iam_policy.article_analysis_lambda_boundary.policy) <= 6144
    )
    error_message = "追加後もIAM容量を守る: ロール作成=${length(aws_iam_policy.apply_role_creation.policy)}, PassRole=${length(aws_iam_policy.apply_pass_role.policy)}, inline=${length(aws_iam_role_policy.apply.policy)}。"
  }
}
