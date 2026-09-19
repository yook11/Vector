locals {
  auth_rate_limit_cleanup_boundary_groups = toset(["AuthRateLimitCleanupLambda", "AuthRateLimitCleanupScheduler"])
  auth_rate_limit_cleanup_boundary_pairing_statements = [
    for key, statement in local.boundary_pairing_statements_by_group : statement
    if contains(local.auth_rate_limit_cleanup_boundary_groups, key)
  ]
}

resource "aws_iam_policy" "auth_rate_limit_cleanup_lambda_boundary" {
  name        = "${var.name_prefix}-auth-rate-limit-cleanup-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the auth rate limit cleanup Lambda execution role."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RdsIamAuthAsCleanup"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector_auth_rate_limit_cleanup"
      },
      {
        Sid      = "WriteCleanupLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-auth-rate-limit-cleanup:*"
      },
      {
        Sid      = "PublishCleanupFailure"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = "arn:aws:sns:${var.region}:${local.account_id}:${var.name_prefix}-alerts"
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.outbox_lambda_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyNetworkManagementFromFunctionCode"
        Effect    = "Deny"
        Action    = local.outbox_lambda_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.auth_rate_limit_cleanup_lambda_arn } }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "auth_rate_limit_cleanup_scheduler_boundary" {
  name        = "${var.name_prefix}-auth-rate-limit-cleanup-scheduler-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the auth rate limit cleanup Scheduler execution role."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeCleanupOnly"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.auth_rate_limit_cleanup_lambda_arn
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "apply_auth_rate_limit_cleanup" {
  name        = "${var.name_prefix}-ci-apply-auth-rate-limit-cleanup"
  path        = "/${var.name_prefix}-ci/"
  description = "Manage only the auth rate limit cleanup Lambda, schedule and alarms."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "ManageCleanupFunction"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration",
          "lambda:GetFunctionCodeSigningConfig", "lambda:ListVersionsByFunction", "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration", "lambda:GetFunctionConcurrency", "lambda:PutFunctionConcurrency",
          "lambda:DeleteFunctionConcurrency", "lambda:GetRuntimeManagementConfig", "lambda:ListTags", "lambda:TagResource",
          "lambda:UntagResource", "lambda:GetFunctionEventInvokeConfig", "lambda:PutFunctionEventInvokeConfig",
          "lambda:UpdateFunctionEventInvokeConfig", "lambda:DeleteFunctionEventInvokeConfig",
        ]
        Resource = local.auth_rate_limit_cleanup_lambda_arn
      },
      {
        Sid      = "ManageCleanupSchedule"
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"]
        Resource = "arn:aws:scheduler:${var.region}:${local.account_id}:schedule/${var.name_prefix}-auth-rate-limit-cleanup/${var.name_prefix}-auth-rate-limit-cleanup"
      },
      {
        Sid      = "ManageCleanupScheduleGroup"
        Effect   = "Allow"
        Action   = ["scheduler:CreateScheduleGroup", "scheduler:GetScheduleGroup", "scheduler:DeleteScheduleGroup", "scheduler:ListTagsForResource", "scheduler:TagResource", "scheduler:UntagResource"]
        Resource = "arn:aws:scheduler:${var.region}:${local.account_id}:schedule-group/${var.name_prefix}-auth-rate-limit-cleanup"
      },
      {
        Sid      = "ManageCleanupAlarms"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource", "cloudwatch:TagResource", "cloudwatch:UntagResource"]
        Resource = "arn:aws:cloudwatch:${var.region}:${local.account_id}:alarm:${var.name_prefix}-auth-rate-limit-cleanup-*"
      },
    ], local.auth_rate_limit_cleanup_boundary_pairing_statements)
  })
}

resource "aws_iam_role_policy_attachment" "apply_auth_rate_limit_cleanup" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_auth_rate_limit_cleanup.arn
}
