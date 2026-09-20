locals {
  completion_outbox_relay_name = "${var.name_prefix}-completion-outbox-relay"
  completion_outbox_relay_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.completion_outbox_relay_name}"
}

resource "aws_cloudwatch_log_group" "completion_outbox_relay" {
  name              = "/aws/lambda/${local.completion_outbox_relay_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "completion_outbox_relay" {
  name                 = "${local.completion_outbox_relay_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.completion_outbox_relay_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "completion_outbox_relay" {
  name = "completion-outbox-relay"
  role = aws_iam_role.completion_outbox_relay.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_outbox_relay"
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.outbox["completion"].arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.completion_outbox_relay.arn}:*"
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.completion_outbox_relay_arn } }
      },
    ]
  })
}

# relayはCloudWatch Logsと標準メトリクスを使い、X-Rayは採用しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "completion_outbox_relay" {
  function_name                  = local.completion_outbox_relay_name
  role                           = aws_iam_role.completion_outbox_relay.arn
  package_type                   = "Image"
  image_uri                      = local.lambda_initial_image_uri
  architectures                  = ["arm64"]
  memory_size                    = 512
  timeout                        = 120
  reserved_concurrent_executions = 1

  tracing_config {
    mode = "PassThrough"
  }

  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.outbox_relay.completion_handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.app["api"].id]
    security_group_ids = [aws_security_group.outbox_relay.id]
  }

  environment {
    variables = {
      ENV                              = "production"
      DATABASE_URL                     = local.backend_db_url["vector_outbox_relay"]
      DB_IAM_AUTH                      = "true"
      SQS_ARTICLE_COMPLETION_QUEUE_URL = aws_sqs_queue.outbox["completion"].url
    }
  }

  depends_on = [
    aws_iam_role_policy.completion_outbox_relay,
    aws_ecr_repository_policy.backend_lambda_pull,
    aws_vpc_security_group_ingress_rule.rds_from_outbox_relay,
    aws_vpc_security_group_egress_rule.outbox_relay_to_rds,
  ]

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_scheduler_schedule_group" "completion_outbox_relay" {
  name = local.completion_outbox_relay_name
}

resource "aws_iam_role" "completion_outbox_relay_scheduler" {
  name                 = "${local.completion_outbox_relay_name}-scheduler"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.completion_outbox_relay_name}-scheduler-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.completion_outbox_relay.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "completion_outbox_relay_scheduler" {
  name = "invoke-completion-outbox-relay"
  role = aws_iam_role.completion_outbox_relay_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = local.completion_outbox_relay_arn
    }]
  })
}

resource "aws_scheduler_schedule" "completion_outbox_relay" {
  name                = local.completion_outbox_relay_name
  group_name          = aws_scheduler_schedule_group.completion_outbox_relay.name
  state               = "ENABLED"
  schedule_expression = "rate(1 minute)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.completion_outbox_relay.arn
    role_arn = aws_iam_role.completion_outbox_relay_scheduler.arn
    input    = "{}"
  }

  depends_on = [aws_iam_role_policy.completion_outbox_relay_scheduler]
}
