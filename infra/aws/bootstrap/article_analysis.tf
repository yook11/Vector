locals {
  # AI分析(curation・assessment・embedding)は共通の実行ロールで動き、DBへは記事分析専用のユーザーだけで接続する。
  article_analysis_stages          = ["curation", "assessment", "embedding"]
  article_analysis_lambda_arns     = [local.curation_consumer_lambda_arn, local.assessment_consumer_lambda_arn, local.embedding_consumer_lambda_arn]
  article_analysis_lambda_role_arn = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-article-analysis-lambda"
  article_analysis_queue_arns      = [for stage in local.article_analysis_stages : "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-article-${stage}"]
  article_analysis_log_group_arns  = [for stage in local.article_analysis_stages : "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-${stage}-consumer:*"]
  article_analysis_ai_key_arns = [
    "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/curation-consumer/gemini-api-key",
    "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/assessment-consumer/deepseek-api-key",
    "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/embedding-consumer/gemini-api-key",
  ]
  article_analysis_role_boundary_groups = {
    ArticleAnalysisLambda = {
      boundary   = aws_iam_policy.article_analysis_lambda_boundary.arn
      role_names = ["${var.name_prefix}-article-analysis-lambda"]
    }
  }
}

resource "aws_iam_policy" "article_analysis_lambda_boundary" {
  name        = "${var.name_prefix}-article-analysis-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the shared article analysis Lambda execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeAnalysisEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = local.article_analysis_queue_arns
      },
      {
        Sid      = "RdsIamAuthAsArticleAnalysis"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector_article_analysis"
      },
      {
        Sid      = "ReadAiProviderKeys"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = local.article_analysis_ai_key_arns
      },
      {
        Sid      = "ReadFrontendNotificationKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/frontend/revalidate-bearer-secret"
      },
      {
        Sid      = "WriteConsumerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = local.article_analysis_log_group_arns
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.article_analysis_lambda_arns } }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}
