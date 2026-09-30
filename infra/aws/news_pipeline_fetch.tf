locals {
  # 外部取得(acquisition・completion)は共通の実行ロールで動き、取得依頼投入(source-dispatch)とは信頼境界が異なるため分ける。
  article_fetch_role_name = "${var.name_prefix}-article-fetch"
}

resource "aws_iam_role" "article_fetch" {
  name                 = "${local.article_fetch_role_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.article_fetch_role_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "article_fetch" {
  name = "article-fetch"
  role = aws_iam_role.article_fetch.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeAcquisitionEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.source_dispatch["acquisition"].arn
      },
      {
        Sid      = "ConsumeCompletionEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = aws_sqs_queue.outbox["completion"].arn
      },
      {
        Sid      = "RdsIamAuthAsCollect"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_collect"
      },
      {
        Sid    = "WriteConsumerLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          for log_group in [aws_cloudwatch_log_group.acquisition_consumer, aws_cloudwatch_log_group.completion_consumer] :
          "${log_group.arn}:*"
        ]
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.outbox_relay_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyEniOperationsFromFunctionCode"
        Effect    = "Deny"
        Action    = local.outbox_relay_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [local.acquisition_consumer_arn, local.completion_consumer_arn] } }
      },
    ]
  })
}
