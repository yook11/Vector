mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:ap-northeast-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/test-policy" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-role" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::test-state" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}

variables {
  name_prefix  = "slice-test"
  github_owner = "test-owner"
  github_repo  = "test-repo"
  root_domain  = "example.com"
}


# 対応付けの取り違えを検出するため、外部取得のboundaryだけ他と異なるARNにする。
override_resource {
  override_during = plan
  target          = aws_iam_policy.article_fetch_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-article-fetch-lambda-boundary" }
}

run "shared_role_requires_article_fetch_boundary" {
  command = plan
  assert {
    condition = (
      local.role_boundary_groups["ArticleFetchLambda"] == {
        boundary   = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-article-fetch-lambda-boundary"
        role_names = ["slice-test-article-fetch-lambda"]
      } &&
      contains(local.managed_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda") &&
      !contains(local.app_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda")
    )
    error_message = "外部取得の共通ロールを専用boundaryと対にし、アプリ反映では渡さない。"
  }
  assert {
    condition = (
      local.boundary_pairing_statements_by_group["ArticleFetchLambda"] == {
        Sid       = "DenyWideBoundaryOnArticleFetchLambdaRoles"
        Effect    = "Deny"
        Action    = "iam:CreateRole"
        Resource  = ["arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda"]
        Condition = { StringNotEquals = { "iam:PermissionsBoundary" = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-article-fetch-lambda-boundary" } }
      } &&
      contains(jsondecode(aws_iam_role_policy.apply.policy).Statement, local.boundary_pairing_statements_by_group["ArticleFetchLambda"]) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_role_creation.policy).Statement :
        s.Sid != "DenyRoleCreationWithoutBoundary" ? true : contains(s.Condition.StringNotEquals["iam:PermissionsBoundary"], "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-article-fetch-lambda-boundary")
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_role_creation.policy).Statement :
        s.Sid != "DenyRoleCreationOutsideKnownRoles" ? true : contains(s.NotResource, "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda")
      ])
    )
    error_message = "CIは外部取得の共通ロールを専用boundary付きでだけ作成できる。"
  }
}

run "boundary_limits_fetch_to_its_queues_logs_and_collect_user" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.article_fetch_lambda_boundary.policy).Statement == [
        { Sid = "ConsumeAcquisitionEvents", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-source-acquisition" },
        { Sid = "ConsumeCompletionEvents", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], Resource = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-completion" },
        { Sid = "RdsIamAuthAsCollect", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_collect" },
        { Sid = "WriteConsumerLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = [
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-acquisition-consumer:*",
          "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-completion-consumer:*",
        ] },
        { Sid = "ManageLambdaNetworkInterfaces", Effect = "Allow", Action = local.outbox_lambda_eni_actions, Resource = "*" },
        { Sid = "DenyEniOperationsFromFunctionCode", Effect = "Deny", Action = local.outbox_lambda_eni_actions, Resource = "*", Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-acquisition-consumer",
          "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-completion-consumer",
        ] } } },
        local.boundary_no_escalation_statement,
      ]
    )
    error_message = "天井を取得・補完の2キュー・2ロググループ・vector_collectに限定し、ENIのコード実行と権限昇格を拒否する。"
  }
  assert {
    condition     = !strcontains(aws_iam_policy.article_fetch_lambda_boundary.policy, "sqs:SendMessage")
    error_message = "外部のHTMLを処理するconsumerには、取得キューを含むどのキューへの送信も許可しない。"
  }
  assert {
    condition     = length(aws_iam_policy.article_fetch_lambda_boundary.policy) <= 6144
    error_message = "外部取得のboundaryはmanaged policyの容量上限を守る: ${length(aws_iam_policy.article_fetch_lambda_boundary.policy)}。"
  }
}

run "pass_role_admits_shared_fetch_role_to_lambda_only" {
  command = plan
  assert {
    condition = (
      contains(local.outbox_service_roles.Lambda.arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda") &&
      !contains(local.outbox_service_roles.Scheduler.arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-article-fetch-lambda") &&
      alltrue([for guard in local.outbox_pass_role_guards : contains(jsondecode(aws_iam_policy.apply_pass_role.policy).Statement, guard)])
    )
    error_message = "外部取得の共通ロールはLambdaにだけ渡せるようにする。"
  }
}
