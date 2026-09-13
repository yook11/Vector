# relay の天井は既存 ECS ロールから独立させる。
resource "aws_iam_policy" "curation_outbox_relay_lambda_boundary" {
  name        = "${var.name_prefix}-curation-outbox-relay-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the Outbox relay Lambda execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
        Resource = "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-article-curation"
      },
      {
        Sid      = "WriteRelayLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-curation-outbox-relay:*"
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
            "lambda:SourceFunctionArn" = local.curation_outbox_relay_lambda_arn
          }
        }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "curation_outbox_relay_scheduler_boundary" {
  name        = "${var.name_prefix}-curation-outbox-relay-scheduler-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the Outbox relay Scheduler execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeRelayOnly"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.curation_outbox_relay_lambda_arn
      },
      local.boundary_no_escalation_statement,
    ]
  })
}
