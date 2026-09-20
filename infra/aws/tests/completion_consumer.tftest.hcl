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
  target          = aws_security_group.completion_consumer
  values          = { id = "sg-00000000000000011" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.outbox_sqs_endpoint
  values          = { id = "sg-00000000000000012" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["completion"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.completion_dlq
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion-dlq" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.completion_consumer
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-completion-consumer-lambda" }
}

run "completion_consumer_receives_with_bounded_execution" {
  command = plan
  assert {
    condition = (
      aws_lambda_function.completion_consumer.timeout == 600 &&
      aws_lambda_function.completion_consumer.memory_size == 1024 &&
      aws_lambda_function.completion_consumer.reserved_concurrent_executions == 5 &&
      aws_lambda_function.completion_consumer.image_config[0].command == tolist(["app.lambda_handlers.completion.handler.handler"]) &&
      aws_lambda_function.completion_consumer.architectures == tolist(["arm64"]) &&
      aws_lambda_function.completion_consumer.environment[0].variables == tomap({
        ENV                              = "production"
        DATABASE_URL                     = local.backend_db_url["vector_collect"]
        DB_IAM_AUTH                      = "true"
        EGRESS_PROXY_URL                 = local.proxy_url
        SQS_ARTICLE_COMPLETION_QUEUE_URL = aws_sqs_queue.outbox["completion"].url
      }) &&
      aws_lambda_event_source_mapping.completion_consumer.enabled &&
      aws_lambda_event_source_mapping.completion_consumer.batch_size == 10 &&
      aws_lambda_event_source_mapping.completion_consumer.maximum_batching_window_in_seconds == 0 &&
      aws_lambda_event_source_mapping.completion_consumer.scaling_config[0].maximum_concurrency == 5 &&
      aws_lambda_event_source_mapping.completion_consumer.function_response_types == toset(["ReportBatchItemFailures"])
    )
    error_message = "補完の時間予算・Collect接続・部分応答を設定し、受信を有効にする。"
  }
}

run "queue_visibility_and_redrive_preserve_retention" {
  command = plan
  assert {
    condition = (
      aws_sqs_queue.outbox["completion"].visibility_timeout_seconds == 3600 &&
      aws_sqs_queue.outbox["completion"].message_retention_seconds == 1209600 &&
      aws_sqs_queue.completion_dlq.message_retention_seconds == 1209600 &&
      aws_sqs_queue.completion_dlq.sqs_managed_sse_enabled &&
      jsondecode(aws_sqs_queue.outbox["completion"].redrive_policy) == {
        deadLetterTargetArn = aws_sqs_queue.completion_dlq.arn
        maxReceiveCount     = 5
      } &&
      jsondecode(aws_sqs_queue_redrive_allow_policy.completion_dlq.redrive_allow_policy).sourceQueueArns == [aws_sqs_queue.outbox["completion"].arn]
    )
    error_message = "既存保持期間を短縮せず、600秒Lambdaの通常可視性と補完専用DLQを接続する。"
  }
}

run "consumer_collect_and_sqs_permissions" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.completion_consumer.policy).Statement[0].Resource == aws_sqs_queue.outbox["completion"].arn &&
      toset(jsondecode(aws_iam_role_policy.completion_consumer.policy).Statement[0].Action) == toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]) &&
      jsondecode(aws_iam_role_policy.completion_consumer.policy).Statement[1].Resource == "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_collect" &&
      !strcontains(aws_iam_role_policy.completion_consumer.policy, "ssm:") &&
      !strcontains(aws_iam_role_policy.completion_consumer.policy, "sqs:SendMessage") &&
      anytrue([for statement in jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement :
        try(statement.Action == "sqs:ChangeMessageVisibility" && statement.Resource == aws_sqs_queue.outbox["completion"].arn && statement.Principal.AWS == aws_iam_role.completion_consumer.arn, false)
      ])
    )
    error_message = "Collectと補完キューに必要な権限だけを実行roleとendpointで許可する。"
  }
}

run "completion_network_has_no_direct_internet_route" {
  command = plan
  assert {
    condition = (
      aws_subnet.completion_consumer.cidr_block == cidrsubnet(var.vpc_cidr, 8, 33) &&
      !aws_subnet.completion_consumer.map_public_ip_on_launch &&
      aws_route_table_association.completion_consumer.route_table_id == aws_route_table.app.id &&
      local.proxy_clients.completion_consumer.cidr == aws_subnet.completion_consumer.cidr_block &&
      local.proxy_clients.completion_consumer.allow_any_domain &&
      local.proxy_clients.completion_consumer.domains == [] &&
      strcontains(local.squid_conf, "http_access allow src_completion_consumer") &&
      aws_vpc_security_group_egress_rule.completion_consumer_to_sqs.referenced_security_group_id == aws_security_group.outbox_sqs_endpoint.id &&
      aws_vpc_security_group_ingress_rule.sqs_from_completion_consumer.referenced_security_group_id == aws_security_group.completion_consumer.id
    )
    error_message = "専用private subnetから取得proxyとSQS endpointへ必要な通信だけを通す。"
  }
}

run "relay_uses_dedicated_db_user_and_runs_every_minute" {
  command = plan
  assert {
    condition = (
      startswith(aws_lambda_function.completion_outbox_relay.environment[0].variables.DATABASE_URL, "postgresql+asyncpg://vector_outbox_relay@") &&
      aws_lambda_function.completion_outbox_relay.environment[0].variables.DB_IAM_AUTH == "true" &&
      toset(flatten([for s in jsondecode(aws_iam_role_policy.completion_outbox_relay.policy).Statement : s.Resource if s.Action == "rds-db:connect" && s.Effect == "Allow"])) == toset([
        "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:${aws_db_instance.this.resource_id}/vector_outbox_relay",
      ])
    )
    error_message = "Completion Relayは専用ユーザーで接続し、旧Appユーザーへの接続を許可しない。"
  }
  assert {
    condition = (
      aws_lambda_function.completion_outbox_relay.image_config[0].command == tolist(["app.lambda_handlers.outbox_relay.completion_handler"]) &&
      aws_lambda_function.completion_outbox_relay.environment[0].variables.DATABASE_URL == local.backend_db_url["vector_outbox_relay"] &&
      aws_lambda_function.completion_outbox_relay.environment[0].variables.DB_IAM_AUTH == "true" &&
      aws_lambda_function.completion_outbox_relay.environment[0].variables.SQS_ARTICLE_COMPLETION_QUEUE_URL == aws_sqs_queue.outbox["completion"].url &&
      aws_lambda_function.completion_outbox_relay.timeout == 120 &&
      aws_lambda_function.completion_outbox_relay.reserved_concurrent_executions == 1 &&
      aws_scheduler_schedule.completion_outbox_relay.state == "ENABLED" &&
      aws_scheduler_schedule.completion_outbox_relay.schedule_expression == "rate(1 minute)" &&
      jsondecode(aws_iam_role_policy.completion_outbox_relay.policy).Statement[1].Action == "sqs:SendMessage" &&
      jsondecode(aws_iam_role_policy.completion_outbox_relay.policy).Statement[1].Resource == aws_sqs_queue.outbox["completion"].arn &&
      anytrue([for statement in jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement :
        try(statement.Action == "sqs:SendMessage" && statement.Resource == aws_sqs_queue.outbox["completion"].arn && statement.Principal.AWS == aws_iam_role.completion_outbox_relay.arn, false)
      ])
    )
    error_message = "Relayは専用DB接続と補完専用送信権限を使い、毎分の送信を有効にする。"
  }
}

