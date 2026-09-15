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
  target          = aws_sqs_queue.outbox["assessment"]
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment", "url" : "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-assessment" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["embedding"]
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.assessment_dlq
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment-dlq" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.assessment_outbox_relay
  values          = { "arn" : "arn:aws:iam::123456789012:role/slice-test/slice-test-assessment-outbox-relay-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.outbox_relay
  values          = { "arn" : "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" }
}


# 初回の基盤準備では、イメージ指定前に処理を開始しない。
run "redrive_preserves_direct_send_boundary" {
  command = plan
  assert {
    condition = alltrue([
      for policy in concat(
        [for queue in aws_sqs_queue_policy.outbox : jsondecode(queue.policy)],
        [jsondecode(aws_sqs_queue_policy.source_dispatch["acquisition"].policy)]
      ) :
      policy.Statement[0].Effect == "Deny" &&
      policy.Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      policy.Statement[1].Effect == "Deny" &&
      policy.Statement[1].Action == "sqs:SendMessage" &&
      policy.Statement[1].Condition.StringNotEquals["aws:sourceVpce"] == "vpce-test" &&
      policy.Statement[1].Condition.StringNotEqualsIfExists["aws:CalledViaLast"] == "sqs.amazonaws.com"
    ])
    error_message = "TLSと直接送信のVPC制限を維持し、SQS代理呼び出しだけを例外にする。"
  }
}
