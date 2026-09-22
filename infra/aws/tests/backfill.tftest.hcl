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
  target          = aws_iam_role.backfill
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-backfill-lambda" }
}
override_resource {
  override_during = plan
  target          = aws_iam_role.backfill_scheduler
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-backfill-scheduler" }
}
override_resource {
  override_during = plan
  target          = aws_scheduler_schedule_group.backfill
  values          = { arn = "arn:aws:scheduler:ap-northeast-1:123456789012:schedule-group/slice-test-backfill" }
}
override_resource {
  override_during = plan
  target          = aws_lambda_function.backfill["curation"]
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-backfill" }
}




override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.backfill["curation"]
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-curation-backfill" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["curation"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-curation" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.backfill["assessment"]
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-backfill" }
}




override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.backfill["assessment"]
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-assessment-backfill" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["assessment"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-assessment" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.backfill["embedding"]
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-embedding-backfill" }
}




override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.backfill["embedding"]
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-embedding-backfill" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["embedding"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-embedding" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.backfill["completion"]
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-completion-backfill" }
}
override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.backfill["completion"]
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-completion-backfill" }
}
override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["completion"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-completion" }
}
override_resource {
  override_during = plan
  target          = aws_security_group.outbox_relay
  values          = { id = "sg-00000000000000010" }
}

run "first_deployment_enables_all_four_stages" {
  command = plan
  assert {
    condition     = toset(keys(aws_lambda_function.backfill)) == toset(["curation", "assessment", "embedding", "completion"]) && alltrue([for schedule in aws_scheduler_schedule.backfill : schedule.state == "ENABLED"])
    error_message = "4工程の初回配置は全工程を有効にする。"
  }
  assert {
    condition = alltrue([for stage, function in aws_lambda_function.backfill :
      function.function_name == "slice-test-${stage}-backfill" &&
      function.image_config[0].command == tolist(["app.lambda_handlers.backfill.${stage}_handler"]) &&
      function.image_config[0].entry_point == tolist(["/app/.venv/bin/python", "-m", "awslambdaric"]) &&
      function.image_config[0].working_directory == "/app" &&
      function.environment[0].variables == tomap({
        ENV                                     = "production"
        DATABASE_URL                            = local.backend_db_url["vector_app"]
        DB_IAM_AUTH                             = "true"
        "SQS_ARTICLE_${upper(stage)}_QUEUE_URL" = aws_sqs_queue.outbox[stage].url
        "BACKFILL_${upper(stage)}S_ENABLED"     = "true"
      })
    ])
    error_message = "自工程の入口と送信先だけをbackendコンテナへ渡す。"
  }
}

run "scheduled_invocations_keep_offsets_and_target_pairings" {
  command = plan
  assert {
    condition = { for stage, schedule in aws_scheduler_schedule.backfill : stage => schedule.schedule_expression } == {
      curation   = "cron(0,30 * * * ? *)"
      assessment = "cron(5,35 * * * ? *)"
      embedding  = "cron(10,40 * * * ? *)"
      completion = "cron(15,45 * * * ? *)"
      } && alltrue([for stage, schedule in aws_scheduler_schedule.backfill :
        schedule.schedule_expression_timezone == "UTC" &&
        schedule.flexible_time_window[0].mode == "OFF" &&
        schedule.target[0].input == "{}" &&
        schedule.target[0].arn == aws_lambda_function.backfill[stage].arn &&
        schedule.target[0].role_arn == aws_iam_role.backfill_scheduler.arn &&
        schedule.group_name == aws_scheduler_schedule_group.backfill.name
    ]) && aws_scheduler_schedule_group.backfill.name == "slice-test-backfill"
    error_message = "UTCで30分間隔と工程別offsetを保ち、共通groupと共通Schedulerロールで対応するLambdaを起動する。"
  }
}

run "both_retry_layers_expire_after_sixty_seconds_without_error_retries" {
  command = plan
  assert {
    condition = alltrue([for stage, schedule in aws_scheduler_schedule.backfill :
      schedule.target[0].retry_policy[0].maximum_retry_attempts == 0 &&
      schedule.target[0].retry_policy[0].maximum_event_age_in_seconds == 60 &&
      aws_lambda_function_event_invoke_config.backfill[stage].maximum_retry_attempts == 0 &&
      aws_lambda_function_event_invoke_config.backfill[stage].maximum_event_age_in_seconds == 60 &&
      aws_lambda_function_event_invoke_config.backfill[stage].function_name == aws_lambda_function.backfill[stage].function_name &&
      length(schedule.target[0].dead_letter_config) == 0 &&
      length(aws_lambda_function_event_invoke_config.backfill[stage].destination_config) == 0
    ])
    error_message = "Scheduler配送とLambda関数エラーの再試行を0回・有効期間60秒にする。"
  }
}

run "backfill_reuses_private_relay_connections_with_bounded_compute" {
  command = plan
  assert {
    condition = alltrue([for function in aws_lambda_function.backfill :
      function.architectures == tolist(["arm64"]) && function.memory_size == 512 &&
      function.timeout == 120 && function.reserved_concurrent_executions == 1 &&
      function.vpc_config[0].subnet_ids == toset([aws_subnet.app["api"].id]) &&
      function.vpc_config[0].security_group_ids == toset([aws_security_group.outbox_relay.id])
    ]) && alltrue([for group in aws_cloudwatch_log_group.backfill : group.retention_in_days == var.log_retention_days])
    error_message = "既存relayと同じprivate接続・実行上限・ログ保持期間を利用する。"
  }
}

run "execution_role_and_endpoint_allow_only_backfill_queues" {
  command = plan
  assert {
    condition = (
      aws_iam_role.backfill.name == "slice-test-backfill-lambda" &&
      aws_iam_role.backfill.permissions_boundary == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-backfill-lambda-boundary" &&
      alltrue([for function in aws_lambda_function.backfill : function.role == aws_iam_role.backfill.arn]) &&
      jsondecode(aws_iam_role_policy.backfill.policy).Statement == [
        { Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_app" },
        { Effect = "Allow", Action = "sqs:SendMessage", Resource = [for stage in ["assessment", "completion", "curation", "embedding"] : aws_sqs_queue.outbox[stage].arn] },
        { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = [for stage in ["assessment", "completion", "curation", "embedding"] : "${aws_cloudwatch_log_group.backfill[stage].arn}:*"] },
        { Effect = "Allow", Action = local.outbox_relay_eni_actions, Resource = "*" },
        { Sid = "DenyEniOperationsFromFunctionCode", Effect = "Deny", Action = local.outbox_relay_eni_actions, Resource = "*", Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [for stage in ["assessment", "completion", "curation", "embedding"] : local.backfill_arns[stage]] } } },
      ] &&
      [for statement in jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement : statement if try(statement.Principal.AWS == aws_iam_role.backfill.arn, false)] == [
        { Effect = "Allow", Principal = { AWS = aws_iam_role.backfill.arn }, Action = "sqs:SendMessage", Resource = [for stage in ["assessment", "completion", "curation", "embedding"] : aws_sqs_queue.outbox[stage].arn] }
      ]
    )
    error_message = "段共通の実行roleとendpointをbackfillの4キューへの送信だけに限定し、DBロールとENI拒否を維持する。"
  }
  assert {
    condition = alltrue([for policy in aws_sqs_queue_policy.outbox :
      anytrue([for statement in jsondecode(policy.policy).Statement : try(statement.Effect == "Deny" && statement.Action == "sqs:SendMessage" && statement.Condition.StringNotEquals["aws:sourceVpce"] == aws_vpc_endpoint.outbox_sqs.id, false)])
    ])
    error_message = "既存キューのendpoint外からの送信拒否を維持する。"
  }
}

run "scheduler_trust_and_invocation_are_backfill_scoped" {
  command = plan
  assert {
    condition = (
      aws_iam_role.backfill_scheduler.name == "slice-test-backfill-scheduler" &&
      aws_iam_role.backfill_scheduler.permissions_boundary == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-backfill-scheduler-boundary" &&
      jsondecode(aws_iam_role.backfill_scheduler.assume_role_policy).Statement == [{
        Effect    = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole",
        Condition = { StringEquals = { "aws:SourceAccount" = "123456789012" }, ArnEquals = { "aws:SourceArn" = aws_scheduler_schedule_group.backfill.arn } }
      }] &&
      jsondecode(aws_iam_role_policy.backfill_scheduler.policy).Statement == [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = [for stage in ["assessment", "completion", "curation", "embedding"] : local.backfill_arns[stage]] }]
    )
    error_message = "Schedulerの信頼元をaccountと共通groupに、呼び出しをbackfillの4関数に限定する。"
  }
}
