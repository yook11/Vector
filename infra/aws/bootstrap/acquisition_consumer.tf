locals {
  acquisition_consumer_eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
}

resource "aws_iam_policy" "acquisition_consumer_lambda_boundary" {
  name        = "${var.name_prefix}-acquisition-consumer-lambda-boundary"
  path        = "/${var.name_prefix}-ci/"
  description = "Ceiling for the acquisition consumer Lambda execution role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeAcquisitionEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = "arn:aws:sqs:${var.region}:${local.account_id}:${var.name_prefix}-source-acquisition"
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
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-acquisition-consumer:*"
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.acquisition_consumer_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyEniOperationsFromFunctionCode"
        Effect    = "Deny"
        Action    = local.acquisition_consumer_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.acquisition_consumer_lambda_arn } }
      },
      local.boundary_no_escalation_statement,
    ]
  })
}

resource "aws_iam_policy" "apply_acquisition_consumer" {
  name        = "${var.name_prefix}-ci-apply-acquisition-consumer"
  path        = "/${var.name_prefix}-ci/"
  description = "Manage only the acquisition Lambda and its event source mappings."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "ManageAcquisitionFunction"
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
        Resource = local.acquisition_consumer_lambda_arn
      },
      {
        Sid      = "CreateAcquisitionMapping"
        Effect   = "Allow"
        Action   = "lambda:CreateEventSourceMapping"
        Resource = "*"
        Condition = {
          ArnEquals    = { "lambda:FunctionArn" = local.acquisition_consumer_lambda_arn }
          StringEquals = { "aws:RequestTag/Consumer" = "${var.name_prefix}-acquisition-consumer" }
        }
      },
      {
        Sid      = "ManageAcquisitionMapping"
        Effect   = "Allow"
        Action   = ["lambda:GetEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping"]
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          ArnEquals    = { "lambda:FunctionArn" = local.acquisition_consumer_lambda_arn }
          StringEquals = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-acquisition-consumer" }
        }
      },
      {
        Sid       = "ReadAcquisitionMappingTags"
        Effect    = "Allow"
        Action    = "lambda:ListTags"
        Resource  = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = { StringEquals = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-acquisition-consumer" } }
      },
      {
        Sid      = "TagAcquisitionMapping"
        Effect   = "Allow"
        Action   = "lambda:TagResource"
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          StringEquals                = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-acquisition-consumer" }
          StringEqualsIfExists        = { "aws:RequestTag/Consumer" = "${var.name_prefix}-acquisition-consumer" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Consumer", "Project", "ManagedBy"] }
        }
      },
      {
        Sid      = "UntagAcquisitionMappingMetadata"
        Effect   = "Allow"
        Action   = "lambda:UntagResource"
        Resource = "arn:aws:lambda:${var.region}:${local.account_id}:event-source-mapping:*"
        Condition = {
          StringEquals                = { "aws:ResourceTag/Consumer" = "${var.name_prefix}-acquisition-consumer" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Project", "ManagedBy"] }
        }
      },
    ], [local.boundary_pairing_statements_by_group["AcquisitionConsumerLambda"]])
  })
}

resource "aws_iam_role_policy_attachment" "apply_acquisition_consumer" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_acquisition_consumer.arn
}
