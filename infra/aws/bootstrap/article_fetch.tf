locals {
  # 外部取得(acquisition・completion)は共通の実行ロールで動き、取得依頼投入(source-dispatch)とは信頼境界が異なるため分ける。
  article_fetch_lambda_arns     = [local.acquisition_consumer_lambda_arn, local.completion_consumer_lambda_arn]
  article_fetch_lambda_role_arn = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-article-fetch-lambda"
  article_fetch_log_group_arns  = [for consumer in ["acquisition", "completion"] : "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-${consumer}-consumer:*"]
  article_fetch_role_boundary_groups = {
    ArticleFetchLambda = {
      boundary   = aws_iam_policy.article_fetch_lambda_boundary.arn
      role_names = ["${var.name_prefix}-article-fetch-lambda"]
    }
  }
}

resource "aws_iam_policy" "article_fetch_lambda_boundary" {
  name        = "${var.name_prefix}-article-fetch-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the shared article fetch Lambda execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeAcquisitionEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = local.source_dispatch_queue_arns["acquisition"]
      },
      {
        Sid      = "ConsumeCompletionEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-article-completion"
      },
      {
        Sid      = "RdsIamAuthAsCollect"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector_collect"
      },
      {
        Sid      = "WriteConsumerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = local.article_fetch_log_group_arns
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.outbox_lambda_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyEniOperationsFromFunctionCode"
        Effect    = "Deny"
        Action    = local.outbox_lambda_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.article_fetch_lambda_arns } }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}
