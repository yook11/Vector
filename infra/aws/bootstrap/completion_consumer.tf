resource "aws_iam_policy" "apply_completion_consumer" {
  name        = "${var.name_prefix}-ci-apply-completion-consumer"
  path        = "/${var.name_prefix}-ci/"
  description = "Manage only the completion Lambda and its event source mappings."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManageCompletionFunction"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction",
          "lambda:GetFunction", "lambda:GetFunctionConfiguration",
          "lambda:GetFunctionCodeSigningConfig", "lambda:ListVersionsByFunction",
          "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunctionConcurrency", "lambda:PutFunctionConcurrency",
          "lambda:DeleteFunctionConcurrency", "lambda:GetRuntimeManagementConfig",
          "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource",
        ]
        Resource = local.completion_consumer_lambda_arn
      },
      {
        Sid      = "CreateCompletionMapping"
        Effect   = "Allow"
        Action   = "lambda:CreateEventSourceMapping"
        Resource = "*"
        Condition = {
          ArnEquals    = { "lambda:FunctionArn" = local.completion_consumer_lambda_arn }
          StringEquals = { "aws:RequestTag/Consumer" = "${var.name_prefix}-completion-consumer" }
        }
      },
      {
        Sid      = "ManageCompletionMapping"
        Effect   = "Allow"
        Action   = ["lambda:GetEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping"]
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          ArnEquals    = { "lambda:FunctionArn" = local.completion_consumer_lambda_arn }
          StringEquals = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-completion-consumer" }
        }
      },
      {
        Sid       = "ReadCompletionMappingTags"
        Effect    = "Allow"
        Action    = "lambda:ListTags"
        Resource  = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = { StringEquals = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-completion-consumer" } }
      },
      {
        Sid      = "TagCompletionMapping"
        Effect   = "Allow"
        Action   = "lambda:TagResource"
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          StringEquals                = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-completion-consumer" }
          StringEqualsIfExists        = { "aws:RequestTag/Consumer" = "${var.name_prefix}-completion-consumer" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Consumer", "Project", "ManagedBy"] }
        }
      },
      {
        Sid      = "UntagCompletionMappingMetadata"
        Effect   = "Allow"
        Action   = "lambda:UntagResource"
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          StringEquals                = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-completion-consumer" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Project", "ManagedBy"] }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "apply_completion_consumer" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_completion_consumer.arn
}
