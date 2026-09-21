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

run "consumer_receives_with_bounded_execution" {
  command = plan
  assert {
    condition = (
      aws_lambda_function.acquisition_consumer.timeout == 300 &&
      aws_lambda_function.acquisition_consumer.memory_size == 1024 &&
      aws_lambda_function.acquisition_consumer.reserved_concurrent_executions == 5 &&
      aws_lambda_function.acquisition_consumer.architectures == tolist(["arm64"]) &&
      aws_lambda_function.acquisition_consumer.image_config[0].command == tolist(["app.lambda_handlers.acquisition.handler.handler"]) &&
      aws_lambda_function.acquisition_consumer.environment[0].variables.CROSSREF_CONTACT_EMAIL == var.crossref_contact_email &&
      aws_lambda_function.acquisition_consumer.environment[0].variables.DB_IAM_AUTH == "true" &&
      aws_lambda_event_source_mapping.acquisition_consumer.enabled &&
      aws_lambda_event_source_mapping.acquisition_consumer.batch_size == 1 &&
      aws_lambda_event_source_mapping.acquisition_consumer.maximum_batching_window_in_seconds == 0 &&
      aws_lambda_event_source_mapping.acquisition_consumer.scaling_config[0].maximum_concurrency == 5 &&
      aws_lambda_event_source_mapping.acquisition_consumer.function_response_types == toset(["ReportBatchItemFailures"])
    )
    error_message = "受信時間・同時実行・有効な受信・取得入口の契約を守る。"
  }
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.lambda_success_stalled["acquisition_consumer"].threshold == 0 &&
      aws_cloudwatch_metric_alarm.lambda_success_stalled["acquisition_consumer"].evaluation_periods == 2 &&
      aws_cloudwatch_metric_alarm.lambda_success_stalled["acquisition_consumer"].comparison_operator == "LessThanOrEqualToThreshold" &&
      aws_cloudwatch_metric_alarm.lambda_success_stalled["acquisition_consumer"].treat_missing_data == "breaching" &&
      alltrue([for query in aws_cloudwatch_metric_alarm.lambda_success_stalled["acquisition_consumer"].metric_query : length(query.metric) == 0 ? true :
        query.metric[0].namespace == "AWS/Lambda" && query.metric[0].period == 3600 && query.metric[0].dimensions.FunctionName == local.acquisition_consumer_name
      ]) &&
      aws_cloudwatch_metric_alarm.lambda_success_stalled["acquisition_consumer"].alarm_actions == toset([aws_sns_topic.alerts.arn]) &&
      aws_cloudwatch_metric_alarm.lambda_success_stalled["acquisition_consumer"].ok_actions == toset([aws_sns_topic.alerts.arn])
    )
    error_message = "取得Consumerの正常完了が2時間ゼロなら受信停止として通知する。"
  }
}
run "queue_redrive_and_failure_dashboard" {
  command = plan
  assert {
    condition = (
      aws_sqs_queue.source_dispatch["acquisition"].visibility_timeout_seconds == 1800 &&
      aws_sqs_queue.source_dispatch["acquisition"].message_retention_seconds == 1209600 &&
      jsondecode(aws_sqs_queue.source_dispatch["acquisition"].redrive_policy).maxReceiveCount == 5 &&
      jsondecode(aws_sqs_queue.source_dispatch["acquisition"].redrive_policy).deadLetterTargetArn == aws_sqs_queue.acquisition_dlq.arn &&
      aws_sqs_queue.acquisition_dlq.sqs_managed_sse_enabled && !aws_sqs_queue.acquisition_dlq.fifo_queue &&
      aws_sqs_queue.acquisition_dlq.message_retention_seconds == 1209600 &&
      jsondecode(aws_sqs_queue_redrive_allow_policy.acquisition_dlq.redrive_allow_policy).sourceQueueArns == [aws_sqs_queue.source_dispatch["acquisition"].arn] &&
      length(jsondecode(aws_cloudwatch_dashboard.source_dispatch.dashboard_body).widgets) == 3
    )
    error_message = "取得Consumer専用DLQと30分の可視性、14日保持を定義する。"
  }
}
run "consumer_permissions_and_network_are_scoped" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.acquisition_consumer.policy).Statement[0].Resource == aws_sqs_queue.source_dispatch["acquisition"].arn &&
      toset(jsondecode(aws_iam_role_policy.acquisition_consumer.policy).Statement[0].Action) == toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]) &&
      endswith(jsondecode(aws_iam_role_policy.acquisition_consumer.policy).Statement[1].Resource, "/vector_collect") &&
      aws_vpc_security_group_egress_rule.acquisition_consumer_to_rds.to_port == 5432 &&
      aws_vpc_security_group_egress_rule.acquisition_consumer_to_proxy.to_port == var.proxy_port &&
      local.proxy_clients.acquisition_consumer.allow_any_domain &&
      length(jsondecode(aws_sqs_queue_policy.acquisition_dlq.policy).Statement) == 1 &&
      jsondecode(aws_sqs_queue_policy.source_dispatch["acquisition"].policy).Statement[1].Action == "sqs:SendMessage"
    )
    error_message = "受信先とCollect接続を限定し、DLQ配送へVPCE条件を課さない。"
  }
}
