mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
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
  mock_resource "aws_subnet" { defaults = { id = "subnet-test" } }
  mock_resource "aws_db_instance" { defaults = { resource_id = "db-TEST", address = "db.example.com" } }
  mock_resource "aws_vpc_endpoint" { defaults = { id = "vpce-test" } }
  mock_resource "aws_route_table" { defaults = { id = "rtb-test" } }
  mock_resource "aws_lb" { defaults = { arn = "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:loadbalancer/app/test/1234567890123456" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:test" } }
  mock_resource "aws_ecr_repository" { defaults = { repository_url = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/test" } }
  mock_resource "aws_sqs_queue" { defaults = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:test", url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/test", redrive_policy = "" } }
  mock_resource "aws_sns_topic" { defaults = { arn = "arn:aws:sns:ap-northeast-1:123456789012:slice-test-alerts" } }
  mock_resource "aws_lambda_function" { defaults = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-auth-rate-limit-cleanup" } }
}

variables {
  name_prefix                          = "slice-test"
  root_domain                          = "example.com"
  frontend_domain                      = "app.example.com"
  crossref_contact_email               = "test@example.com"
  slack_team_id                        = "T0123456789"
  slack_channel_id                     = "C0123456789"
  auth_rate_limit_cleanup_image_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

run "cleanup_is_deployed_stopped_with_bounded_runtime" {
  command = plan
  assert {
    condition = (
      length(aws_lambda_function.auth_rate_limit_cleanup) == 1 &&
      aws_lambda_function.auth_rate_limit_cleanup[0].architectures == tolist(["arm64"]) &&
      aws_lambda_function.auth_rate_limit_cleanup[0].memory_size == 512 &&
      aws_lambda_function.auth_rate_limit_cleanup[0].timeout == 30 &&
      aws_lambda_function.auth_rate_limit_cleanup[0].reserved_concurrent_executions == 1 &&
      aws_lambda_function.auth_rate_limit_cleanup[0].image_config[0].command == tolist(["app.lambda_handlers.auth_rate_limit_cleanup.handler.handler"]) &&
      aws_lambda_function.auth_rate_limit_cleanup[0].environment[0].variables["DATABASE_URL"] == local.backend_db_url["vector_auth_rate_limit_cleanup"] &&
      aws_scheduler_schedule.auth_rate_limit_cleanup[0].state == "DISABLED"
    )
    error_message = "専用DB接続の掃除Lambdaをarm64/512MiB/30秒/同時実行1で停止配置する。"
  }
}

run "schedule_and_async_delivery_match_the_retry_contract" {
  command = plan
  assert {
    condition = (
      aws_scheduler_schedule.auth_rate_limit_cleanup[0].schedule_expression == "cron(20,50 * * * ? *)" &&
      aws_scheduler_schedule.auth_rate_limit_cleanup[0].schedule_expression_timezone == "UTC" &&
      aws_scheduler_schedule.auth_rate_limit_cleanup[0].flexible_time_window[0].mode == "OFF" &&
      aws_scheduler_schedule.auth_rate_limit_cleanup[0].target[0].input == "{}" &&
      aws_scheduler_schedule.auth_rate_limit_cleanup[0].target[0].retry_policy[0].maximum_retry_attempts == 0 &&
      aws_lambda_function_event_invoke_config.auth_rate_limit_cleanup[0].maximum_retry_attempts == 2 &&
      aws_lambda_function_event_invoke_config.auth_rate_limit_cleanup[0].maximum_event_age_in_seconds == 600 &&
      aws_lambda_function_event_invoke_config.auth_rate_limit_cleanup[0].destination_config[0].on_failure[0].destination == aws_sns_topic.alerts.arn
    )
    error_message = "毎時20/50分の入力固定scheduleとLambda非同期再試行・失敗通知を構成する。"
  }
}

run "iam_and_alarms_are_cleanup_scoped" {
  command = plan
  assert {
    condition = (
      [for s in jsondecode(aws_iam_role_policy.auth_rate_limit_cleanup.policy).Statement : s.Resource if s.Action == "rds-db:connect"] == ["arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_auth_rate_limit_cleanup"] &&
      toset(keys(aws_cloudwatch_metric_alarm.auth_rate_limit_cleanup)) == toset(["async_events_dropped", "destination_failure", "scheduler_failure"])
    )
    error_message = "IAM DB接続先を専用ユーザーに限定し、破棄・通知失敗・配信失敗を監視する。"
  }
}
