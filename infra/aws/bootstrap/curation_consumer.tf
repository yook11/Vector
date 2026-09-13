locals {
  curation_consumer_eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
}

resource "aws_iam_policy" "curation_consumer_lambda_boundary" {
  name        = "${var.name_prefix}-curation-consumer-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the curation consumer Lambda execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeCurationEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-article-curation"
      },
      {
        Sid      = "RdsIamAuthAsApp"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector_app"
      },
      {
        Sid      = "ReadGeminiKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/curation-consumer/gemini-api-key"
      },
      {
        Sid      = "WriteConsumerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-curation-consumer:*"
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.curation_consumer_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyEniOperationsFromFunctionCode"
        Effect    = "Deny"
        Action    = local.curation_consumer_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.curation_consumer_lambda_arn } }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "apply_curation_consumer" {
  name        = "${var.name_prefix}-ci-apply-curation-consumer"
  path        = "/${var.name_prefix}-ci/"
  description = "Manage only the curation Lambda and its event source mappings."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "ManageCurationFunction"
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
        Resource = local.curation_consumer_lambda_arn
      },
      {
        Sid      = "CreateCurationMapping"
        Effect   = "Allow"
        Action   = "lambda:CreateEventSourceMapping"
        Resource = "*"
        Condition = {
          ArnEquals    = { "lambda:FunctionArn" = local.curation_consumer_lambda_arn }
          StringEquals = { "aws:RequestTag/Consumer" = "${var.name_prefix}-curation-consumer" }
        }
      },
      {
        Sid      = "ManageCurationMapping"
        Effect   = "Allow"
        Action   = ["lambda:GetEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping"]
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          ArnEquals    = { "lambda:FunctionArn" = local.curation_consumer_lambda_arn }
          StringEquals = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-curation-consumer" }
        }
      },
      {
        Sid       = "ReadCurationMappingTags"
        Effect    = "Allow"
        Action    = "lambda:ListTags"
        Resource  = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = { StringEquals = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-curation-consumer" } }
      },
      {
        Sid      = "TagCurationMapping"
        Effect   = "Allow"
        Action   = "lambda:TagResource"
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          StringEquals                = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-curation-consumer" }
          StringEqualsIfExists        = { "aws:RequestTag/Consumer" = "${var.name_prefix}-curation-consumer" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Consumer", "Project", "ManagedBy"] }
        }
      },
      {
        Sid      = "UntagCurationMappingMetadata"
        Effect   = "Allow"
        Action   = "lambda:UntagResource"
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          StringEquals                = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-curation-consumer" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Project", "ManagedBy"] }
        }
      },
    ], local.curation_boundary_pairing_statements)
  })
}

resource "aws_iam_role_policy_attachment" "apply_curation_consumer" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_curation_consumer.arn
}
