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
  mock_data "aws_ecr_image" {
    defaults = { image_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
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

run "lambda_backend_images_do_not_expire" {
  command = plan

  assert {
    condition     = !contains(keys(aws_ecr_lifecycle_policy.this), "backend")
    error_message = "Lambdaが参照中のbackendイメージを件数で自動削除しない。"
  }

  assert {
    condition     = contains(keys(aws_ecr_repository.this), "backend") && aws_ecr_repository.this["backend"].image_tag_mutability == "IMMUTABLE"
    error_message = "backendリポジトリとcommitタグの不変性を維持する。"
  }
}

run "backend_image_pull_is_limited_to_prefixed_lambdas" {
  command = plan

  assert {
    condition = (
      length(jsondecode(aws_ecr_repository_policy.backend_lambda_pull.policy).Statement) == 1 &&
      jsondecode(aws_ecr_repository_policy.backend_lambda_pull.policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:lambda:${var.region}:123456789012:function:slice-test-*" &&
      jsondecode(aws_ecr_repository_policy.backend_lambda_pull.policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_ecr_repository_policy.backend_lambda_pull.policy).Statement[0].Principal.Service == "lambda.amazonaws.com" &&
      toset(jsondecode(aws_ecr_repository_policy.backend_lambda_pull.policy).Statement[0].Action) == toset(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"])
    )
    error_message = "backend ECRからの取得は、同一アカウントで名前がprefixに従うLambdaだけに許可する。"
  }
}

run "lambdas_start_from_the_latest_backend_image" {
  command = plan

  assert {
    condition = alltrue([for image_uri in concat(
      [for function in aws_lambda_function.backfill : function.image_uri],
      [
        aws_lambda_function.outbox_relay.image_uri,
        aws_lambda_function.curation_outbox_relay.image_uri,
        aws_lambda_function.assessment_outbox_relay.image_uri,
        aws_lambda_function.completion_outbox_relay.image_uri,
        aws_lambda_function.curation_consumer.image_uri,
        aws_lambda_function.assessment_consumer.image_uri,
        aws_lambda_function.embedding_consumer.image_uri,
        aws_lambda_function.completion_consumer.image_uri,
        aws_lambda_function.acquisition_consumer.image_uri,
        aws_lambda_function.source_dispatch.image_uri,
        aws_lambda_function.auth_rate_limit_cleanup.image_uri,
      ]) :
      image_uri == "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/test@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ])
    error_message = "Lambdaの新規作成はbackendの最新イメージをdigestで参照し、版の前進はrolloutに任せる。"
  }
}

run "other_repositories_keep_configured_retention" {
  command = plan

  variables {
    ecr_retained_images = 3
  }

  assert {
    condition = (
      toset(keys(aws_ecr_lifecycle_policy.this)) == setsubtract(toset(keys(aws_ecr_repository.this)), toset(["backend"])) &&
      alltrue([for policy in aws_ecr_lifecycle_policy.this :
        jsondecode(policy.policy).rules[0].selection.countNumber == 3 &&
        jsondecode(policy.policy).rules[0].selection.tagStatus == "any" &&
        jsondecode(policy.policy).rules[0].selection.countType == "imageCountMoreThan" &&
        jsondecode(policy.policy).rules[0].action.type == "expire"
      ])
    )
    error_message = "backend以外のリポジトリは指定件数による保持を維持する。"
  }
}
