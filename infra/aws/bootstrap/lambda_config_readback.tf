data "aws_kms_key" "lambda_config" {
  key_id = "alias/aws/lambda"
}

locals {
  lambda_config_function_arns = [local.outbox_lambda_arn, local.embedding_consumer_lambda_arn, local.assessment_outbox_relay_lambda_arn, local.assessment_consumer_lambda_arn]
  lambda_kms_service          = "lambda.${var.region}.amazonaws.com"
}

resource "aws_iam_policy" "lambda_config_readback" {
  name        = "${var.name_prefix}-ci-lambda-config-readback"
  path        = "/${var.name_prefix}-ci/"
  description = "Decrypt only pipeline Lambda configuration through regional Lambda."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid         = "DenyDecryptOtherKeys"
        Effect      = "Deny"
        Action      = "kms:Decrypt"
        NotResource = data.aws_kms_key.lambda_config.arn
      },
      {
        Sid      = "DenyDecryptOutsideLambda"
        Effect   = "Deny"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_key.lambda_config.arn
        Condition = {
          StringNotEquals = { "kms:ViaService" = local.lambda_kms_service }
        }
      },
      {
        Sid      = "DenyDecryptOtherFunctions"
        Effect   = "Deny"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_key.lambda_config.arn
        Condition = {
          ArnNotEquals = { "kms:EncryptionContext:aws:lambda:FunctionArn" = local.lambda_config_function_arns }
        }
      },
      {
        Sid      = "ReadPipelineLambdaConfiguration"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = data.aws_kms_key.lambda_config.arn
        Condition = {
          StringEquals = { "kms:ViaService" = local.lambda_kms_service }
          ArnEquals    = { "kms:EncryptionContext:aws:lambda:FunctionArn" = local.lambda_config_function_arns }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_config_readback" {
  for_each = toset(["plan", "apply"])

  role       = aws_iam_role.ci[each.key].name
  policy_arn = aws_iam_policy.lambda_config_readback.arn
}
