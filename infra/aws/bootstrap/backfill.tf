locals {
  backfill_stages      = toset(["curation", "assessment", "embedding", "completion"])
  backfill_lambda_arns = { for stage in local.backfill_stages : stage => "arn:aws:lambda:${var.region}:${local.account_id}:function:${var.name_prefix}-${stage}-backfill" }
  # 救済(backfill)は段共通の実行ロール・Schedulerロールで動く。段を足すときは列挙に加えるだけで許可表は伸びない。
  backfill_lambda_role_arn    = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-backfill-lambda"
  backfill_scheduler_role_arn = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-backfill-scheduler"
  backfill_schedule_group_arn = "arn:aws:scheduler:${var.region}:${local.account_id}:schedule-group/${var.name_prefix}-backfill"
  backfill_schedule_arns      = [for stage in local.backfill_stages : "arn:aws:scheduler:${var.region}:${local.account_id}:schedule/${var.name_prefix}-backfill/${var.name_prefix}-${stage}-backfill"]
  backfill_queue_arns         = [for stage in local.backfill_stages : "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-article-${stage}"]
  backfill_log_group_arns     = [for stage in local.backfill_stages : "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-${stage}-backfill:*"]
  # DeleteScheduleGroupはgroup配下の全scheduleを消すため、schedule/<group>/*へのDeleteScheduleも要る。
  backfill_schedule_group_wildcard_arn = "arn:aws:scheduler:${var.region}:${local.account_id}:schedule/${var.name_prefix}-backfill/*"
  backfill_role_boundary_groups = {
    BackfillLambda = {
      boundary   = aws_iam_policy.backfill_lambda_boundary.arn
      role_names = ["${var.name_prefix}-backfill-lambda"]
    }
    BackfillScheduler = {
      boundary   = aws_iam_policy.backfill_scheduler_boundary.arn
      role_names = ["${var.name_prefix}-backfill-scheduler"]
    }
  }
  backfill_boundary_pairing_statements = [for key, statement in local.boundary_pairing_statements_by_group : statement if contains(keys(local.backfill_role_boundary_groups), key)]
}

# 段別boundaryの撤去後に段共通boundaryを元のラベルへ戻す。本番へ適用済みになったら削除してよい。
moved {
  from = aws_iam_policy.backfill_lambda_boundary_shared
  to   = aws_iam_policy.backfill_lambda_boundary
}

moved {
  from = aws_iam_policy.backfill_scheduler_boundary_shared
  to   = aws_iam_policy.backfill_scheduler_boundary
}

resource "aws_iam_policy" "backfill_lambda_boundary" {
  name        = "${var.name_prefix}-backfill-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the backfill Lambda execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RdsIamAuthAsBackfill"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector_backfill"
      },
      {
        Sid      = "RdsIamAuthAsApp"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector_app"
      },
      {
        Sid      = "SendPipelineEvents"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = local.backfill_queue_arns
      },
      {
        Sid      = "WriteBackfillLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = local.backfill_log_group_arns
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.outbox_lambda_eni_actions
        Resource = "*"
      },
      {
        Sid      = "DenyNetworkManagementFromFunctionCode"
        Effect   = "Deny"
        Action   = local.outbox_lambda_eni_actions
        Resource = "*"
        Condition = {
          ArnEquals = {
            "lambda:SourceFunctionArn" = values(local.backfill_lambda_arns)
          }
        }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "backfill_scheduler_boundary" {
  name        = "${var.name_prefix}-backfill-scheduler-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the backfill Scheduler execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeBackfillOnly"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = values(local.backfill_lambda_arns)
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "apply_backfill" {
  name        = "${var.name_prefix}-ci-apply-backfill"
  path        = "/${var.name_prefix}-ci/"
  description = "Manage only backfill functions, asynchronous invocation settings and schedules."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "ManageBackfillFunctions"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction",
          "lambda:GetFunction", "lambda:GetFunctionConfiguration",
          "lambda:GetFunctionCodeSigningConfig", "lambda:ListVersionsByFunction",
          "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunctionConcurrency", "lambda:PutFunctionConcurrency",
          "lambda:DeleteFunctionConcurrency", "lambda:GetRuntimeManagementConfig",
          "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource",
          "lambda:GetFunctionEventInvokeConfig", "lambda:PutFunctionEventInvokeConfig",
          "lambda:UpdateFunctionEventInvokeConfig", "lambda:DeleteFunctionEventInvokeConfig",
        ]
        Resource = values(local.backfill_lambda_arns)
      },
      {
        Sid      = "ManageBackfillSchedules"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"]
        Resource = concat(local.backfill_schedule_arns, [local.backfill_schedule_group_wildcard_arn])
      },
      {
        Sid      = "ManageBackfillScheduleGroups"
        Effect   = "Allow"
        Action   = ["scheduler:CreateScheduleGroup", "scheduler:GetScheduleGroup", "scheduler:DeleteScheduleGroup", "scheduler:ListTagsForResource", "scheduler:TagResource", "scheduler:UntagResource"]
        Resource = [local.backfill_schedule_group_arn]
      },
    ], local.backfill_boundary_pairing_statements)
  })
}

resource "aws_iam_role_policy_attachment" "apply_backfill" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_backfill.arn
}
