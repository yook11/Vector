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

# 共有ロールと旧ロールを取り違えていないことを示すため、ロールごとに別のARNを与える。
override_resource {
  override_during = plan
  target          = aws_iam_role.outbox_relay
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.outbox_relay_scheduler
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.completion_outbox_relay
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-completion-outbox-relay-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.curation_outbox_relay
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-curation-outbox-relay-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.assessment_outbox_relay
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-assessment-outbox-relay-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.completion_outbox_relay_scheduler
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-completion-outbox-relay-scheduler" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.curation_outbox_relay_scheduler
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-curation-outbox-relay-scheduler" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.assessment_outbox_relay_scheduler
  values          = { arn = "arn:aws:iam::123456789012:role/slice-test/slice-test-assessment-outbox-relay-scheduler" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["assessment"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["completion"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["curation"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["embedding"]
  values          = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.outbox_relay
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-outbox-relay" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.completion_outbox_relay
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-completion-outbox-relay" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.curation_outbox_relay
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-curation-outbox-relay" }
}

override_resource {
  override_during = plan
  target          = aws_cloudwatch_log_group.assessment_outbox_relay
  values          = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-assessment-outbox-relay" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.outbox_relay
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-outbox-relay" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.completion_outbox_relay
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-completion-outbox-relay" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.curation_outbox_relay
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-outbox-relay" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.assessment_outbox_relay
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-outbox-relay" }
}

run "relays_run_on_the_outbox_relay_role" {
  command = plan
  assert {
    condition = [
      for relay in [aws_lambda_function.outbox_relay, aws_lambda_function.completion_outbox_relay, aws_lambda_function.curation_outbox_relay, aws_lambda_function.assessment_outbox_relay] :
      { name = relay.function_name, role = relay.role }
      ] == [
      { name = "slice-test-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" },
      { name = "slice-test-completion-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" },
      { name = "slice-test-curation-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" },
      { name = "slice-test-assessment-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" },
    ]
    error_message = "配信の4本は汎用relayの実行ロールで動く。"
  }
}

run "relay_schedules_share_the_outbox_relay_group" {
  command = plan
  assert {
    condition = [
      for schedule in [aws_scheduler_schedule.outbox_relay, aws_scheduler_schedule.completion_outbox_relay, aws_scheduler_schedule.curation_outbox_relay, aws_scheduler_schedule.assessment_outbox_relay] :
      { name = schedule.name, group = schedule.group_name, role = schedule.target[0].role_arn, target = schedule.target[0].arn, rate = schedule.schedule_expression, state = schedule.state }
      ] == [
      { name = "slice-test-outbox-relay", group = "slice-test-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler", target = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-outbox-relay", rate = "rate(1 minute)", state = "ENABLED" },
      { name = "slice-test-completion-outbox-relay", group = "slice-test-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler", target = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-completion-outbox-relay", rate = "rate(1 minute)", state = "ENABLED" },
      { name = "slice-test-curation-outbox-relay", group = "slice-test-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler", target = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-outbox-relay", rate = "rate(1 minute)", state = "ENABLED" },
      { name = "slice-test-assessment-outbox-relay", group = "slice-test-outbox-relay", role = "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler", target = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-outbox-relay", rate = "rate(1 minute)", state = "ENABLED" },
    ]
    error_message = "4本のscheduleは汎用relayのgroupとSchedulerロールで、それぞれのrelayを毎分起動する。"
  }
}

run "outbox_relay_role_covers_every_relay" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role_policy.outbox_relay.policy).Statement == [
      { Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_outbox_relay" },
      {
        Effect = "Allow", Action = "sqs:SendMessage",
        Resource = [
          "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment",
          "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion",
          "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation",
          "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding",
        ]
      },
      {
        Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = [
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-outbox-relay:*",
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-completion-outbox-relay:*",
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-curation-outbox-relay:*",
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-assessment-outbox-relay:*",
        ]
      },
      { Effect = "Allow", Action = local.outbox_relay_eni_actions, Resource = "*" },
      {
        Sid = "DenyEniOperationsFromFunctionCode", Effect = "Deny", Action = local.outbox_relay_eni_actions, Resource = "*",
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-outbox-relay",
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-completion-outbox-relay",
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-outbox-relay",
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-outbox-relay",
        ] } }
      },
    ]
    error_message = "汎用relayの実行ロールは専用DBユーザー・4キュー・4本のログを持ち、4本の関数コードからのENI操作を拒否する。"
  }
  assert {
    condition = [
      for s in jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement : s.Resource
      if s.Principal.AWS == "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda"
      ] == [[
        "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment",
        "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion",
        "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation",
        "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding",
    ]]
    error_message = "VPC endpointは汎用relayの実行ロールに4キューへの送信を許可する。"
  }
}

run "outbox_relay_scheduler_invokes_every_relay" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role_policy.outbox_relay_scheduler.policy).Statement == [{
      Effect = "Allow", Action = "lambda:InvokeFunction",
      Resource = [
        "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-outbox-relay",
        "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-completion-outbox-relay",
        "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-outbox-relay",
        "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-outbox-relay",
      ]
    }]
    error_message = "汎用relayのSchedulerロールは4本のrelayだけを起動する。"
  }
}
