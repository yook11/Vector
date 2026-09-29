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

run "relay_lambda_ceiling_follows_naming_convention" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.outbox_relay_lambda_boundary.policy).Statement == [
        { Sid = "RdsIamAuthAsOutboxRelay", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_outbox_relay" },
        { Sid = "SendPipelineEvents", Effect = "Allow", Action = "sqs:SendMessage", Resource = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-*" },
        { Sid = "DenySendToDeadLetterQueues", Effect = "Deny", Action = "sqs:SendMessage", Resource = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-*-dlq" },
        { Sid = "WriteRelayLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-*outbox-relay:*" },
        { Sid = "ManageLambdaNetworkInterfaces", Effect = "Allow", Action = local.outbox_lambda_eni_actions, Resource = "*" },
        { Sid = "DenyNetworkManagementFromFunctionCode", Effect = "Deny", Action = local.outbox_lambda_eni_actions, Resource = "*", Condition = { ArnLike = { "lambda:SourceFunctionArn" = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-*outbox-relay" } } },
        local.boundary_no_escalation_statement,
      ]
    )
    error_message = "配信の天井を工程キュー・relayのログと専用DBユーザーに限定し、DLQへの送信・ENIのコード実行・権限昇格を拒否する。"
  }
}

run "relay_scheduler_ceiling_invokes_only_relays" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.outbox_relay_scheduler_boundary.policy).Statement == [
        { Sid = "InvokeRelayOnly", Effect = "Allow", Action = "lambda:InvokeFunction", Resource = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-*outbox-relay" },
        local.boundary_no_escalation_statement,
      ]
    )
    error_message = "Schedulerの天井をrelay関数の起動だけに限定する。"
  }
}

# IAMの*を正規表現の.*に置き換え、命名規則が既存のrelay・キュー・DLQを取り違えないことを確かめる。
run "relay_naming_convention_covers_existing_relays" {
  command = plan
  assert {
    condition = alltrue([
      for arn in [local.outbox_lambda_arn, local.assessment_outbox_relay_lambda_arn, local.curation_outbox_relay_lambda_arn, local.completion_outbox_relay_lambda_arn] :
      can(regex("^arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-.*outbox-relay$", arn))
    ])
    error_message = "4本のrelay関数がすべて命名規則のパターンに収まる。"
  }
  assert {
    condition = alltrue([
      for arn in local.outbox_queue_arns :
      can(regex("^arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-.*$", arn)) &&
      !can(regex("^arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-.*-dlq$", arn))
    ])
    error_message = "4つの工程キューは送信を許可し、DLQの拒否には当たらない。"
  }
  assert {
    condition = alltrue([
      for arn in [local.completion_dlq_arn, local.curation_dlq_arn, local.assessment_dlq_arn, local.embedding_dlq_arn] :
      can(regex("^arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-.*-dlq$", arn))
    ])
    error_message = "4つの工程のDLQはすべて送信拒否に当たる。"
  }
}

run "ci_manages_relay_schedules_per_group" {
  command = plan
  assert {
    condition = (
      [for s in jsondecode(aws_iam_policy.apply_outbox.policy).Statement : s if s.Sid == "ManageOutboxSchedule"] == [{
        Sid    = "ManageOutboxSchedule", Effect = "Allow",
        Action = ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"],
        Resource = [
          "arn:aws:scheduler:ap-northeast-1:123456789012:schedule/slice-test-outbox-relay/*",
          "arn:aws:scheduler:ap-northeast-1:123456789012:schedule/slice-test-assessment-outbox-relay/*",
          "arn:aws:scheduler:ap-northeast-1:123456789012:schedule/slice-test-curation-outbox-relay/*",
          "arn:aws:scheduler:ap-northeast-1:123456789012:schedule/slice-test-completion-outbox-relay/*",
        ]
      }]
    )
    error_message = "scheduleの管理をrelayの4 groupの配下に限定し、group間の移動とgroup削除を通す。"
  }
}
