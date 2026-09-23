mock_provider "aws" {
  mock_resource "aws_subnet" {
    defaults = { id = "subnet-00000000000000001" }
  }

  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:test", last_modified = "2026-09-10T00:00:00.000+0000" }
  }

  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:ap-northeast-1:123456789012:certificate/test"
      domain_validation_options = [{
        domain_name           = "app.example.com"
        resource_record_name  = "_test.app.example.com"
        resource_record_type  = "CNAME"
        resource_record_value = "_test.acm-validations.aws."
      }]
    }
  }
  mock_resource "aws_db_instance" {
    defaults = { resource_id = "db-TEST", address = "db.example.com" }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = { id = "vpce-test" }
  }
  mock_resource "aws_route_table" {
    defaults = { id = "rtb-test" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:loadbalancer/app/test/1234567890123456" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-role" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:test" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      redrive_policy = ""
      arn            = "arn:aws:sqs:ap-northeast-1:123456789012:test"
      url            = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/test"
    }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-northeast-1:123456789012:test-alerts" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:ap-northeast-1:123456789012:repository/test", repository_url = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/test" }
  }
}

variables {
  name_prefix            = "slice-test"
  root_domain            = "example.com"
  frontend_domain        = "app.example.com"
  crossref_contact_email = "test@example.com"
  slack_team_id          = "T0123456789"
  slack_channel_id       = "C0123456789"
}

override_resource {
  override_during = plan
  target          = aws_iam_role.article_analysis
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-article-analysis-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["curation"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["assessment"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["embedding"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.curation_consumer
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-curation-consumer" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.assessment_consumer
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-assessment-consumer" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.embedding_consumer
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-embedding-consumer" }
}

run "consumers_share_role_and_connect_as_article_analysis" {
  command = plan
  assert {
    condition = (
      aws_iam_role.article_analysis.name == "slice-test-article-analysis-lambda" &&
      aws_iam_role.article_analysis.path == "/slice-test/" &&
      aws_iam_role.article_analysis.permissions_boundary == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-article-analysis-lambda-boundary" &&
      jsondecode(aws_iam_role.article_analysis.assume_role_policy).Statement == [
        { Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" },
      ]
    )
    error_message = "共通ロールをAI分析のboundaryに固定し、Lambdaだけが引き受けられるようにする。"
  }
  assert {
    condition = alltrue([
      for function in [aws_lambda_function.curation_consumer, aws_lambda_function.assessment_consumer, aws_lambda_function.embedding_consumer] :
      function.role == aws_iam_role.article_analysis.arn &&
      function.environment[0].variables.DATABASE_URL == local.backend_db_url["vector_article_analysis"] &&
      startswith(function.environment[0].variables.DATABASE_URL, "postgresql+asyncpg://vector_article_analysis@")
    ])
    error_message = "3つのConsumerは共通ロールで動き、記事分析専用のDBユーザーで接続する。"
  }
}

run "execution_policy_allows_only_article_analysis_resources" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.article_analysis.policy).Statement == [
        { Sid = "ConsumeAnalysisEvents", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = [for stage in ["curation", "assessment", "embedding"] : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-${stage}"] },
        { Sid = "RdsIamAuthAsArticleAnalysis", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_article_analysis" },
        { Sid = "ReadAiProviderKeys", Effect = "Allow", Action = "ssm:GetParameter", Resource = [
          "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/curation-consumer/gemini-api-key",
          "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/assessment-consumer/deepseek-api-key",
          "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/embedding-consumer/gemini-api-key",
        ] },
        { Sid = "ReadFrontendNotificationKey", Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/frontend/revalidate-bearer-secret" },
        { Sid = "WriteConsumerLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = [for stage in ["curation", "assessment", "embedding"] : "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-${stage}-consumer:*"] },
        { Sid = "ManageLambdaNetworkInterfaces", Effect = "Allow", Action = local.outbox_relay_eni_actions, Resource = "*" },
        { Sid = "DenyEniOperationsFromFunctionCode", Effect = "Deny", Action = local.outbox_relay_eni_actions, Resource = "*", Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [for stage in ["curation", "assessment", "embedding"] : "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-${stage}-consumer"] } } },
      ]
    )
    error_message = "実行権限をAI分析の3キュー・3つのAIキーと通知キー・3ロググループに限定し、関数コードからのENI操作を拒否する。"
  }
  assert {
    condition = toset(flatten([
      for s in jsondecode(aws_iam_role_policy.article_analysis.policy).Statement : s.Resource
      if s.Action == "rds-db:connect" && s.Effect == "Allow"
    ])) == toset(["arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_article_analysis"])
    error_message = "共通ロールは記事分析専用のDBユーザーだけに接続でき、vector_appへは接続できない。"
  }
}
