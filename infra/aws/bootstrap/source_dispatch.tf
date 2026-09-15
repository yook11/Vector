locals {
  source_dispatch_lambda_arn         = "arn:aws:lambda:${var.region}:${local.account_id}:function:${var.name_prefix}-source-dispatch"
  source_dispatch_lambda_role_arn    = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-source-dispatch-lambda"
  source_dispatch_scheduler_role_arn = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-source-dispatch-scheduler"
  source_dispatch_queue_arns = {
    acquisition       = "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-source-acquisition"
    scheduler_failure = "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-source-dispatch-scheduler-dlq"
    execution_failure = "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-source-dispatch-execution-failures"
  }
  source_dispatch_role_boundary_groups = {
    SourceDispatchLambda = {
      boundary   = aws_iam_policy.source_dispatch_lambda_boundary.arn
      role_names = ["${var.name_prefix}-source-dispatch-lambda"]
    }
    SourceDispatchScheduler = {
      boundary   = aws_iam_policy.source_dispatch_scheduler_boundary.arn
      role_names = ["${var.name_prefix}-source-dispatch-scheduler"]
    }
  }
  source_dispatch_boundary_pairing_statements = [for key, statement in local.boundary_pairing_statements_by_group : statement if contains(keys(local.source_dispatch_role_boundary_groups), key)]
}

resource "aws_iam_policy" "source_dispatch_lambda_boundary" {
  name        = "${var.name_prefix}-source-dispatch-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the source_dispatch Lambda execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RdsIamAuthAsApp"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector_collect"
      },
      {
        Sid      = "SendPipelineEvents"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = [local.source_dispatch_queue_arns["acquisition"], local.source_dispatch_queue_arns["execution_failure"]]
      },
      {
        Sid      = "WriteSourceDispatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-source-dispatch:*"
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
            "lambda:SourceFunctionArn" = local.source_dispatch_lambda_arn
          }
        }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "source_dispatch_scheduler_boundary" {
  name        = "${var.name_prefix}-source-dispatch-scheduler-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the source_dispatch Scheduler execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeSourceDispatchOnly"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.source_dispatch_lambda_arn
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = local.source_dispatch_queue_arns["scheduler_failure"]
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "apply_source_dispatch" {
  name        = "${var.name_prefix}-ci-apply-source-dispatch"
  path        = "/${var.name_prefix}-ci/"
  description = "Manage only source_dispatch functions, asynchronous invocation settings and schedules."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "ManageSourceDispatchFunctions"
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
        Resource = [local.source_dispatch_lambda_arn]
      },
      {
        Sid      = "ManageSourceDispatchSchedules"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"]
        Resource = [for cadence in ["high", "medium", "low"] : "arn:aws:scheduler:${var.region}:${local.account_id}:schedule/${var.name_prefix}-source-dispatch/${var.name_prefix}-source-dispatch-${cadence}"]
      },
      {
        Sid      = "ManageSourceDispatchScheduleGroups"
        Effect   = "Allow"
        Action   = ["scheduler:CreateScheduleGroup", "scheduler:GetScheduleGroup", "scheduler:DeleteScheduleGroup", "scheduler:ListTagsForResource", "scheduler:TagResource", "scheduler:UntagResource"]
        Resource = ["arn:aws:scheduler:${var.region}:${local.account_id}:schedule-group/${var.name_prefix}-source-dispatch"]
      },
    ], local.source_dispatch_boundary_pairing_statements)
  })
}

resource "aws_iam_role_policy_attachment" "apply_source_dispatch" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_source_dispatch.arn
}
