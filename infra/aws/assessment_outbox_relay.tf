locals {
  assessment_outbox_relay_name = "${var.name_prefix}-assessment-outbox-relay"
  assessment_outbox_relay_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.assessment_outbox_relay_name}"
}

resource "aws_cloudwatch_log_group" "assessment_outbox_relay" {
  name              = "/aws/lambda/${local.assessment_outbox_relay_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "assessment_outbox_relay" {
  name                 = "${local.assessment_outbox_relay_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.assessment_outbox_relay_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "assessment_outbox_relay" {
  name = "assessment-outbox-relay"
  role = aws_iam_role.assessment_outbox_relay.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_app"
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.outbox["assessment"].arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.assessment_outbox_relay.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = local.outbox_relay_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyEniOperationsFromFunctionCode"
        Effect    = "Deny"
        Action    = local.outbox_relay_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.assessment_outbox_relay_arn } }
      },
    ]
  })
}

# relayはCloudWatch Logsと標準メトリクスを使い、X-Rayは採用しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "assessment_outbox_relay" {
  count = var.assessment_outbox_relay_image_digest == null ? 0 : 1

  function_name                  = local.assessment_outbox_relay_name
  role                           = aws_iam_role.assessment_outbox_relay.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.this["backend"].repository_url}@${var.assessment_outbox_relay_image_digest}"
  architectures                  = ["arm64"]
  memory_size                    = 512
  timeout                        = 120
  reserved_concurrent_executions = 1

  tracing_config {
    mode = "PassThrough"
  }

  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.outbox_relay.assessment_handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.app["api"].id]
    security_group_ids = [aws_security_group.outbox_relay.id]
  }

  environment {
    variables = {
      ENV                              = "production"
      DATABASE_URL                     = local.backend_db_url["vector_app"]
      DB_IAM_AUTH                      = "true"
      SQS_ARTICLE_ASSESSMENT_QUEUE_URL = aws_sqs_queue.outbox["assessment"].url
    }
  }

  depends_on = [
    aws_iam_role_policy.assessment_outbox_relay,
    aws_ecr_repository_policy.outbox_relay,
    aws_vpc_security_group_ingress_rule.rds_from_outbox_relay,
    aws_vpc_security_group_egress_rule.outbox_relay_to_rds,
  ]
}

resource "aws_scheduler_schedule_group" "assessment_outbox_relay" {
  name = local.assessment_outbox_relay_name
}

resource "aws_iam_role" "assessment_outbox_relay_scheduler" {
  name                 = "${local.assessment_outbox_relay_name}-scheduler"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.assessment_outbox_relay_name}-scheduler-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.assessment_outbox_relay.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "assessment_outbox_relay_scheduler" {
  name = "invoke-assessment-outbox-relay"
  role = aws_iam_role.assessment_outbox_relay_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = local.assessment_outbox_relay_arn
    }]
  })
}

resource "aws_scheduler_schedule" "assessment_outbox_relay" {
  count = var.assessment_outbox_relay_image_digest == null ? 0 : 1

  name                = local.assessment_outbox_relay_name
  group_name          = aws_scheduler_schedule_group.assessment_outbox_relay.name
  state               = "ENABLED"
  schedule_expression = "rate(1 minute)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.assessment_outbox_relay[0].arn
    role_arn = aws_iam_role.assessment_outbox_relay_scheduler.arn
    input    = "{}"
  }

  depends_on = [aws_iam_role_policy.assessment_outbox_relay_scheduler]
}
