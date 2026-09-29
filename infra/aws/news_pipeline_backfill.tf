locals {
  backfill_stages = {
    curation = {
      schedule = "cron(0,30 * * * ? *)"
    }
    assessment = {
      schedule = "cron(5,35 * * * ? *)"
    }
    embedding = {
      schedule = "cron(10,40 * * * ? *)"
    }
    completion = {
      schedule = "cron(15,45 * * * ? *)"
    }
  }
  backfill_names = { for stage in keys(local.backfill_stages) : stage => "${var.name_prefix}-${stage}-backfill" }
  backfill_arns  = { for stage, name in local.backfill_names : stage => "arn:aws:lambda:${var.region}:${local.account_id}:function:${name}" }
  # 救済(backfill)は段共通のロールで動く。段を足すときは backfill_stages に加えるだけで、ロールと boundary は増えない。
  backfill_role_name = "${var.name_prefix}-backfill"
}

resource "aws_cloudwatch_log_group" "backfill" {
  for_each = local.backfill_stages

  name              = "/aws/lambda/${local.backfill_names[each.key]}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "backfill" {
  name                 = "${local.backfill_role_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.backfill_role_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "backfill" {
  name = "backfill"
  role = aws_iam_role.backfill.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_backfill"
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = [for stage in keys(local.backfill_stages) : aws_sqs_queue.outbox[stage].arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for stage in keys(local.backfill_stages) : "${aws_cloudwatch_log_group.backfill[stage].arn}:*"]
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = values(local.backfill_arns) } }
      },
    ]
  })
}

# backfillはCloudWatch Logsと標準メトリクスを使用する。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "backfill" {
  for_each = local.backfill_stages

  function_name                  = local.backfill_names[each.key]
  role                           = aws_iam_role.backfill.arn
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
    command           = ["app.lambda_handlers.backfill.${each.key}_handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.app["api"].id]
    security_group_ids = [aws_security_group.outbox_relay.id]
  }

  environment {
    variables = {
      ENV                                        = "production"
      DATABASE_URL                               = local.backend_db_url["vector_backfill"]
      DB_IAM_AUTH                                = "true"
      "SQS_ARTICLE_${upper(each.key)}_QUEUE_URL" = aws_sqs_queue.outbox[each.key].url
      "BACKFILL_${upper(each.key)}S_ENABLED"     = "true"
    }
  }

  depends_on = [
    aws_iam_role_policy.backfill,
    aws_ecr_repository_policy.backend_lambda_pull,
    aws_vpc_security_group_ingress_rule.rds_from_outbox_relay,
    aws_vpc_security_group_egress_rule.outbox_relay_to_rds,
  ]

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_scheduler_schedule_group" "backfill" {
  name = local.backfill_role_name
}

resource "aws_iam_role" "backfill_scheduler" {
  name                 = "${local.backfill_role_name}-scheduler"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.backfill_role_name}-scheduler-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.backfill.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "backfill_scheduler" {
  name = "invoke-backfill"
  role = aws_iam_role.backfill_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = values(local.backfill_arns)
    }]
  })
}

resource "aws_scheduler_schedule" "backfill" {
  for_each = local.backfill_stages

  name                         = local.backfill_names[each.key]
  group_name                   = aws_scheduler_schedule_group.backfill.name
  state                        = "ENABLED"
  schedule_expression          = each.value.schedule
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.backfill[each.key].arn
    role_arn = aws_iam_role.backfill_scheduler.arn
    input    = "{}"

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }

  depends_on = [aws_iam_role_policy.backfill_scheduler, aws_lambda_function_event_invoke_config.backfill]
}

resource "aws_lambda_function_event_invoke_config" "backfill" {
  for_each = local.backfill_stages

  function_name                = aws_lambda_function.backfill[each.key].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}
