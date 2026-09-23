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

override_resource {
  override_during = plan
  target          = aws_iam_policy.backfill_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-backfill-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.backfill_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-backfill-scheduler-boundary" }
}

run "backfill_roles_require_their_own_boundary" {
  command = plan
  assert {
    condition = (
      length(local.backfill_role_boundary_groups) == 2 &&
      local.role_boundary_groups["BackfillLambda"].boundary == aws_iam_policy.backfill_lambda_boundary.arn &&
      local.role_boundary_groups["BackfillLambda"].role_names == ["slice-test-backfill-lambda"] &&
      local.role_boundary_groups["BackfillScheduler"].boundary == aws_iam_policy.backfill_scheduler_boundary.arn &&
      local.role_boundary_groups["BackfillScheduler"].role_names == ["slice-test-backfill-scheduler"] &&
      alltrue([for statement in local.backfill_boundary_pairing_statements :
        statement.Effect == "Deny" && statement.Action == "iam:CreateRole" &&
        contains(jsondecode(aws_iam_policy.apply_backfill.policy).Statement, statement) &&
        !contains(local.inline_boundary_pairing_statements, statement)
      ])
    )
    error_message = "段共通の2ロールを専用boundaryに固定し、CIの作成制約を取り付ける。"
  }
}

run "lambda_boundary_limits_database_queues_logs_and_eni" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.backfill_lambda_boundary.policy).Statement == [
        { Sid = "RdsIamAuthAsApp", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_app" },
        { Sid = "SendPipelineEvents", Effect = "Allow", Action = "sqs:SendMessage", Resource = [for stage in ["assessment", "completion", "curation", "embedding"] : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-${stage}"] },
        { Sid = "WriteBackfillLogs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = [for stage in ["assessment", "completion", "curation", "embedding"] : "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-${stage}-backfill:*"] },
        { Sid = "ManageLambdaNetworkInterfaces", Effect = "Allow", Action = local.outbox_lambda_eni_actions, Resource = "*" },
        { Sid = "DenyNetworkManagementFromFunctionCode", Effect = "Deny", Action = local.outbox_lambda_eni_actions, Resource = "*", Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [for stage in ["assessment", "completion", "curation", "embedding"] : local.backfill_lambda_arns[stage]] } } },
        local.boundary_no_escalation_statement,
      ]
    )
    error_message = "実行権限の天井をbackfillの4キュー・4ロググループとvector_appに限定し、ENIコード実行と権限昇格を拒否する。"
  }
}

run "scheduler_boundary_invokes_only_backfill_functions" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.backfill_scheduler_boundary.policy).Statement == [
        { Sid = "InvokeBackfillOnly", Effect = "Allow", Action = "lambda:InvokeFunction", Resource = [for stage in ["assessment", "completion", "curation", "embedding"] : local.backfill_lambda_arns[stage]] },
        local.boundary_no_escalation_statement,
      ]
    )
    error_message = "Schedulerの天井にはbackfillの4関数のInvokeFunctionのみを許可する。"
  }
}

run "ci_can_manage_async_settings_only_on_backfill_functions" {
  command = plan
  assert {
    condition = (
      alltrue([for s in jsondecode(aws_iam_policy.apply_backfill.policy).Statement : s.Sid != "ManageBackfillFunctions" ? true :
        toset(s.Resource) == toset(values(local.backfill_lambda_arns)) &&
        toset(s.Action) == toset([
          "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration",
          "lambda:GetFunctionCodeSigningConfig", "lambda:ListVersionsByFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunctionConcurrency", "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency", "lambda:GetRuntimeManagementConfig",
          "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource",
          "lambda:GetFunctionEventInvokeConfig", "lambda:PutFunctionEventInvokeConfig", "lambda:UpdateFunctionEventInvokeConfig", "lambda:DeleteFunctionEventInvokeConfig",
        ])
      ]) && aws_iam_role_policy_attachment.apply_backfill.role == aws_iam_role.ci["apply"].name &&
      aws_iam_role_policy_attachment.apply_backfill.policy_arn == aws_iam_policy.apply_backfill.arn
    )
    error_message = "CIの関数管理と非同期設定のCRUDは3工程だけを許可する。"
  }
  assert {
    condition = (
      alltrue([for arn in values(local.backfill_lambda_arns) : contains(local.lambda_config_function_arns, arn)]) &&
      aws_iam_role_policy_attachment.plan_read_only.role == aws_iam_role.ci["plan"].name &&
      aws_iam_role_policy_attachment.plan_read_only.policy_arn == "arn:aws:iam::aws:policy/ReadOnlyAccess"
    )
    error_message = "planは既存read-only policyで読み取り、環境変数の復号も対象関数に限定する。"
  }
}

run "ci_schedule_management_is_limited_to_backfill_groups" {
  command = plan
  assert {
    condition = (
      [for s in jsondecode(aws_iam_policy.apply_backfill.policy).Statement : s if s.Sid == "ManageBackfillSchedules"] == [{
        Sid    = "ManageBackfillSchedules", Effect = "Allow",
        Action = ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"],
        Resource = concat(
          [for stage in ["assessment", "completion", "curation", "embedding"] : "arn:aws:scheduler:ap-northeast-1:123456789012:schedule/slice-test-backfill/slice-test-${stage}-backfill"],
          ["arn:aws:scheduler:ap-northeast-1:123456789012:schedule/slice-test-backfill/*"],
        )
      }]
    )
    error_message = "scheduleのCRUDを共通groupのbackfill名に限定し、group削除に要するgroup配下のDeleteScheduleを含める。"
  }
  assert {
    condition = (
      [for s in jsondecode(aws_iam_policy.apply_backfill.policy).Statement : s if s.Sid == "ManageBackfillScheduleGroups"] == [{
        Sid      = "ManageBackfillScheduleGroups", Effect = "Allow",
        Action   = ["scheduler:CreateScheduleGroup", "scheduler:GetScheduleGroup", "scheduler:DeleteScheduleGroup", "scheduler:ListTagsForResource", "scheduler:TagResource", "scheduler:UntagResource"],
        Resource = ["arn:aws:scheduler:ap-northeast-1:123456789012:schedule-group/slice-test-backfill"]
      }]
    )
    error_message = "groupの作成・削除・参照・タグ管理を共通groupだけに限定する。"
  }
}

run "pass_role_keeps_service_pairing_and_rollout_separation" {
  command = plan
  assert {
    condition = (
      alltrue([for arn in [local.backfill_lambda_role_arn] :
        contains(local.outbox_service_roles.Lambda.arns, arn) && !contains(local.outbox_service_roles.Scheduler.arns, arn) &&
        contains(local.managed_role_arns, arn) && !contains(local.app_role_arns, arn)
        ]) && alltrue([for arn in [local.backfill_scheduler_role_arn] :
        contains(local.outbox_service_roles.Scheduler.arns, arn) && !contains(local.outbox_service_roles.Lambda.arns, arn) &&
        contains(local.managed_role_arns, arn) && !contains(local.app_role_arns, arn)
      ]) && alltrue([for guard in local.outbox_pass_role_guards : contains(jsondecode(aws_iam_policy.apply_pass_role.policy).Statement, guard)])
    )
    error_message = "PassRoleのサービス対応を保ち、app rolloutロールにはbackfillを追加しない。"
  }
}

run "backfill_policies_stay_within_iam_size_limits" {
  command = plan
  assert {
    condition = (
      length(aws_iam_policy.apply_backfill.policy) <= 6144 && length(aws_iam_policy.apply_pass_role.policy) <= 6144 &&
      length(aws_iam_policy.lambda_config_readback.policy) <= 6144 && length(aws_iam_role_policy.apply.policy) <= 10240 &&
      length(aws_iam_policy.backfill_lambda_boundary.policy) <= 6144 &&
      length(aws_iam_policy.backfill_scheduler_boundary.policy) <= 6144
    )
    error_message = "追加後もIAM容量を守る: backfill=${length(aws_iam_policy.apply_backfill.policy)}, PassRole=${length(aws_iam_policy.apply_pass_role.policy)}, inline=${length(aws_iam_role_policy.apply.policy)}。"
  }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.agent_task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-agent-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.migration_task_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-migration-task-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.execution_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-execution-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.migration_execution_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-migration-execution-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-outbox-relay-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.assessment_outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-assessment-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.assessment_outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-assessment-outbox-relay-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.chatbot_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-chatbot-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.agentcore_gateway_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-agentcore-gateway-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.curation_outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-curation-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.curation_outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-curation-outbox-relay-scheduler-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.completion_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-completion-consumer-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.completion_outbox_relay_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-completion-outbox-relay-lambda-boundary" }
}

override_resource {
  override_during = plan
  target          = aws_iam_policy.completion_outbox_relay_scheduler_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-completion-outbox-relay-scheduler-boundary" }
}

run "role_creation_guards_survive_policy_relocation" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_policy.apply_role_creation.policy).Statement == [
        { Sid = "DenyRoleCreationWithoutBoundary", Effect = "Deny", Action = ["iam:CreateRole", "iam:CreateUser"], Resource = "*",
        Condition = { StringNotEquals = { "iam:PermissionsBoundary" = [for group in local.role_boundary_groups : group.boundary] } } },
        { Sid = "DenyRoleCreationOutsideKnownRoles", Effect = "Deny", Action = "iam:CreateRole", NotResource = local.managed_role_arns },
      ] &&
      length(aws_iam_policy.apply_role_creation.policy) <= 6144 &&
      aws_iam_role_policy_attachment.apply_role_creation.role == aws_iam_role.ci["apply"].name &&
      aws_iam_role_policy_attachment.apply_role_creation.policy_arn == aws_iam_policy.apply_role_creation.arn
    )
    error_message = "境界なし・許可表外のロール作成を引き続き拒否する（容量=${length(aws_iam_policy.apply_role_creation.policy)}）。"
  }
}
