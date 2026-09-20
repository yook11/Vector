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

run "private_database_role_network" {
  command = plan
  assert {
    condition     = aws_vpc_endpoint.db_roles_secrets.service_name == "com.amazonaws.ap-northeast-1.secretsmanager" && aws_vpc_endpoint.db_roles_secrets.private_dns_enabled && toset(aws_vpc_endpoint.db_roles_secrets.subnet_ids) == toset([aws_subnet.migration.id])
    error_message = "管理taskはmigration subnetの専用Secrets Manager endpointを利用する。"
  }
  assert {
    condition     = length(aws_vpc_security_group_egress_rule.db_roles) == 3 && aws_vpc_security_group_egress_rule.db_roles["database"].from_port == 5432 && aws_vpc_security_group_egress_rule.db_roles["images_logs"].from_port == 443 && aws_vpc_security_group_egress_rule.db_roles["secrets"].from_port == 443 && aws_vpc_security_group_egress_rule.db_roles_s3.from_port == 443
    error_message = "通信はDB・image/log・secret endpoint・S3 HTTPSに限定する。"
  }
  assert {
    condition     = jsondecode(aws_vpc_endpoint.db_roles_secrets.policy).Statement[0].Condition.ArnEquals["aws:PrincipalArn"] == "arn:aws:iam::123456789012:role/slice-test-db-admin/slice-test-db-roles-exec" && aws_cloudwatch_log_group.db_roles.name == "/ecs/slice-test-db-roles"
    error_message = "secret endpointは専用execution roleだけを許可する。"
  }
}
