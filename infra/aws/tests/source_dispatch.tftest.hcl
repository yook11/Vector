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
  target          = aws_iam_role.source_dispatch
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-source-dispatch-lambda" }
}
override_resource {
  override_during = plan
  target          = aws_iam_role.source_dispatch_scheduler
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-source-dispatch-scheduler" }
}
override_resource {
  override_during = plan
  target          = aws_scheduler_schedule_group.source_dispatch
  values          = { arn = "arn:aws:scheduler:ap-northeast-1:123456789012:schedule-group/slice-test-source-dispatch" }
}
override_resource {
  override_during = plan
  target          = aws_lambda_function.source_dispatch
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-source-dispatch" }
}
override_resource {
  override_during = plan
  target          = aws_sqs_queue.source_dispatch["acquisition"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-source-acquisition", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-source-acquisition" }
}
override_resource {
  override_during = plan
  target          = aws_sqs_queue.source_dispatch["scheduler_failure"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-source-dispatch-scheduler-dlq", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-source-dispatch-scheduler-dlq" }
}
override_resource {
  override_during = plan
  target          = aws_sqs_queue.source_dispatch["execution_failure"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-source-dispatch-execution-failures", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-source-dispatch-execution-failures" }
}

run "dispatch_schedules_are_enabled_and_connected" {
  command = plan
  assert {
    condition = length(aws_scheduler_schedule.source_dispatch) == 3 && alltrue([for cadence, schedule in aws_scheduler_schedule.source_dispatch :
      schedule.state == "ENABLED" && schedule.schedule_expression_timezone == "UTC" && schedule.flexible_time_window[0].mode == "OFF" &&
      schedule.schedule_expression == { high = "cron(0,15,30,45 * * * ? *)", medium = "cron(0 * * * ? *)", low = "cron(0 0,6,12,18 * * ? *)" }[cadence] &&
      schedule.target[0].input == "{\"cadence\":\"${cadence}\",\"scheduled_at\":\"<aws.scheduler.scheduled-time>\"}" &&
      schedule.target[0].arn == aws_lambda_function.source_dispatch.arn && schedule.target[0].role_arn == aws_iam_role.source_dispatch_scheduler.arn &&
      schedule.target[0].retry_policy[0].maximum_retry_attempts == 2 && schedule.target[0].retry_policy[0].maximum_event_age_in_seconds == 600 &&
      schedule.target[0].dead_letter_config[0].arn == aws_sqs_queue.source_dispatch["scheduler_failure"].arn
    ])
    error_message = "UTCの予定回と元の時刻、配送再試行、専用DLQを維持し、3つの予定を有効にする。"
  }
  assert {
    condition = (
      aws_lambda_function.source_dispatch.timeout == 120 && aws_lambda_function.source_dispatch.reserved_concurrent_executions == 3 &&
      aws_lambda_function.source_dispatch.memory_size == 512 && one(aws_lambda_function.source_dispatch.architectures) == "arm64" &&
      one(aws_lambda_function.source_dispatch.image_config[0].command) == "app.lambda_handlers.source_dispatch.handler.handler" &&
      aws_lambda_function.source_dispatch.environment[0].variables["DB_IAM_AUTH"] == "true" &&
      aws_lambda_function.source_dispatch.environment[0].variables["DATABASE_URL"] == local.backend_db_url["vector_collect"] &&
      aws_lambda_function.source_dispatch.environment[0].variables["SQS_SOURCE_ACQUISITION_QUEUE_URL"] == aws_sqs_queue.source_dispatch["acquisition"].url &&
      !contains(keys(aws_lambda_function.source_dispatch.environment[0].variables), "AWS_REGION") &&
      aws_lambda_function_event_invoke_config.source_dispatch.maximum_retry_attempts == 2 &&
      aws_lambda_function_event_invoke_config.source_dispatch.maximum_event_age_in_seconds == 21600 &&
      aws_lambda_function_event_invoke_config.source_dispatch.destination_config[0].on_failure[0].destination == aws_sqs_queue.source_dispatch["execution_failure"].arn
    )
    error_message = "LambdaはIAM接続・逐次投入を行い、内部失敗を専用キューへ保存する。予約変数は上書きしない。"
  }
  assert {
    condition     = alltrue([for key, queue in aws_sqs_queue.source_dispatch : !queue.fifo_queue && queue.sqs_managed_sse_enabled && queue.message_retention_seconds == 1209600 && (key == "acquisition" || queue.visibility_timeout_seconds == 30)])
    error_message = "通常・失敗キューをStandard、暗号化、14日保持とする。"
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.source_dispatch.policy).Statement[0].Resource == "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_collect" &&
      toset(jsondecode(aws_iam_role_policy.source_dispatch.policy).Statement[1].Resource) == toset([aws_sqs_queue.source_dispatch["acquisition"].arn, aws_sqs_queue.source_dispatch["execution_failure"].arn]) &&
      jsondecode(aws_iam_role_policy.source_dispatch_scheduler.policy).Statement[0].Resource == aws_lambda_function.source_dispatch.arn &&
      jsondecode(aws_iam_role_policy.source_dispatch_scheduler.policy).Statement[1].Resource == aws_sqs_queue.source_dispatch["scheduler_failure"].arn &&
      jsondecode(aws_iam_role.source_dispatch_scheduler.assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == aws_scheduler_schedule_group.source_dispatch.arn &&
      jsondecode(aws_iam_role.source_dispatch_scheduler.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    )
    error_message = "ロールの配送先・DBユーザー・Scheduler引受元を限定する。"
  }
  assert {
    condition = (
      anytrue([for s in jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement : try(s.Principal.AWS == aws_iam_role.source_dispatch.arn && s.Action == "sqs:SendMessage" && s.Resource == aws_sqs_queue.source_dispatch["acquisition"].arn, false)]) &&
      jsondecode(aws_sqs_queue_policy.source_dispatch["acquisition"].policy).Statement[1].Condition.StringNotEquals["aws:sourceVpce"] == aws_vpc_endpoint.outbox_sqs.id &&
      alltrue([for key in ["scheduler_failure", "execution_failure"] : length(jsondecode(aws_sqs_queue_policy.source_dispatch[key].policy).Statement) == 1 && jsondecode(aws_sqs_queue_policy.source_dispatch[key].policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"])
    )
    error_message = "通常の送信はVPCEに限定し、AWSの失敗記録配送にはVPCE制限をかけない。"
  }
  assert {
    condition = alltrue([for index, key in ["scheduler_failure", "execution_failure"] :
      jsondecode(aws_cloudwatch_dashboard.source_dispatch.dashboard_body).widgets[index].properties.metrics[0][1] == "ApproximateNumberOfMessagesVisible" &&
      jsondecode(aws_cloudwatch_dashboard.source_dispatch.dashboard_body).widgets[index].properties.metrics[1][1] == "ApproximateAgeOfOldestMessage" &&
      jsondecode(aws_cloudwatch_dashboard.source_dispatch.dashboard_body).widgets[index].properties.metrics[0][3] == aws_sqs_queue.source_dispatch[key].name
    ])
    error_message = "両保存先の標準件数・経過時間をダッシュボードで確認できる。"
  }
}
