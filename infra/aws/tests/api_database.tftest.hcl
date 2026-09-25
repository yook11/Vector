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

run "api_connects_as_api_role_during_switch" {
  command = plan

  assert {
    condition     = local.stage_environment["api"].DATABASE_URL == local.backend_db_url["vector_api"]
    error_message = "api段はvector_apiで接続する。"
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.task["api"].policy).Statement[0].Resource == [
      "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_app",
      "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_api",
    ]
    error_message = "切替前のタスクが入れ替わるまで、api段はvector_appにも接続できる。"
  }
}

run "other_backend_stages_keep_app_role" {
  command = plan

  assert {
    condition = alltrue([for stage in ["insights", "agent"] :
      local.stage_environment[stage].DATABASE_URL == local.backend_db_url["vector_app"]
      && jsondecode(aws_iam_role_policy.task[stage].policy).Statement[0].Resource == [
        "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_app",
      ]
    ])
    error_message = "insights・agent段の接続はvector_appのまま変えない。"
  }
}
