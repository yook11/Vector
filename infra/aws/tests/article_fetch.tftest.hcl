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
  target          = aws_iam_role.article_fetch
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.source_dispatch["acquisition"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-source-acquisition" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["completion"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.acquisition_consumer
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-acquisition-consumer" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.completion_consumer
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-completion-consumer" }
}

run "consumers_share_role_and_connect_as_collect" {
  command = plan
  assert {
    condition = (
      aws_iam_role.article_fetch.name == "slice-test-article-fetch-lambda" &&
      aws_iam_role.article_fetch.path == "/slice-test/" &&
      aws_iam_role.article_fetch.permissions_boundary == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-article-fetch-lambda-boundary" &&
      jsondecode(aws_iam_role.article_fetch.assume_role_policy).Statement == [
        { Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" },
      ]
    )
    error_message = "共通ロールを外部取得のboundaryに固定し、Lambdaだけが引き受けられるようにする。"
  }
  assert {
    condition = alltrue([
      for function in [aws_lambda_function.acquisition_consumer, aws_lambda_function.completion_consumer] :
      function.role == "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda" &&
      function.environment[0].variables.DATABASE_URL == local.backend_db_url["vector_collect"] &&
      startswith(function.environment[0].variables.DATABASE_URL, "postgresql+asyncpg://vector_collect@")
    ])
    error_message = "取得と補完のConsumerは共通ロールで動き、vector_collectで接続する。"
  }
}

run "execution_policy_allows_only_article_fetch_resources" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.article_fetch.policy).Statement == [
        { Sid = "ConsumeAcquisitionEvents", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-source-acquisition" },
        { Sid = "ConsumeCompletionEvents", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], Resource = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion" },
        { Sid = "RdsIamAuthAsCollect", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_collect" },
        { Sid = "WriteConsumerLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = [
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-acquisition-consumer:*",
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-completion-consumer:*",
        ] },
        { Sid = "ManageLambdaNetworkInterfaces", Effect = "Allow", Action = local.outbox_relay_eni_actions, Resource = "*" },
        { Sid = "DenyEniOperationsFromFunctionCode", Effect = "Deny", Action = local.outbox_relay_eni_actions, Resource = "*", Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-acquisition-consumer",
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-completion-consumer",
        ] } } },
      ]
    )
    error_message = "実行権限を取得・補完の2キュー・vector_collect・2ロググループに限定し、関数コードからのENI操作を拒否する。"
  }
  assert {
    condition = (
      !strcontains(aws_iam_role_policy.article_fetch.policy, "sqs:SendMessage") &&
      !strcontains(aws_iam_role_policy.article_fetch.policy, "ssm:")
    )
    error_message = "外部のHTMLを処理するConsumerには、キューへの送信とSSMの参照を許可しない。"
  }
}

run "endpoint_lets_fetch_role_extend_completion_visibility" {
  command = plan
  assert {
    condition = [
      for s in jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement : s
      if s.Action == "sqs:ChangeMessageVisibility"
      ] == [{
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda" }
        Action    = "sqs:ChangeMessageVisibility"
        Resource  = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion"
    }]
    error_message = "endpointは共通ロールに補完キューの可視性変更だけを許可する。"
  }
}
