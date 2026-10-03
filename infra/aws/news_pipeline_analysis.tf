locals {
  # AI分析(curation・assessment・embedding)は共通の実行ロールで動き、DBへは記事分析専用のユーザーだけで接続する。
  article_analysis_role_name = "${var.name_prefix}-article-analysis"
}

resource "aws_iam_role" "article_analysis" {
  name                 = "${local.article_analysis_role_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.article_analysis_role_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "article_analysis" {
  name = "article-analysis"
  role = aws_iam_role.article_analysis.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeAnalysisEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = [for stage in ["curation", "assessment", "embedding"] : aws_sqs_queue.outbox[stage].arn]
      },
      {
        Sid      = "RdsIamAuthAsArticleAnalysis"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_article_analysis"
      },
      {
        Sid    = "ReadAiProviderKeys"
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = [
          for path in [local.curation_consumer_parameter_path, local.assessment_consumer_parameter_path, local.embedding_consumer_parameter_path] :
          "arn:aws:ssm:${var.region}:${local.account_id}:parameter${path}"
        ]
      },
      {
        Sid      = "ReadFrontendNotificationKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.assessment_notification_parameter_path}"
      },
      {
        Sid    = "WriteConsumerLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          for log_group in [aws_cloudwatch_log_group.curation_consumer, aws_cloudwatch_log_group.assessment_consumer, aws_cloudwatch_log_group.embedding_consumer] :
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = [local.curation_consumer_arn, local.assessment_consumer_arn, local.embedding_consumer_arn] } }
      },
    ]
  })
}
