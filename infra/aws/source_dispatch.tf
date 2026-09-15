locals {
  source_dispatch_name = "${var.name_prefix}-source-dispatch"
  source_dispatch_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.source_dispatch_name}"
  source_dispatch_schedules = {
    high   = "cron(0,15,30,45 * * * ? *)"
    medium = "cron(0 * * * ? *)"
    low    = "cron(0 0,6,12,18 * * ? *)"
  }
  source_dispatch_queue_names = {
    acquisition       = "${var.name_prefix}-source-acquisition"
    scheduler_failure = "${local.source_dispatch_name}-scheduler-dlq"
    execution_failure = "${local.source_dispatch_name}-execution-failures"
  }
}

variable "source_dispatch_image_digest" {
  type        = string
  default     = null
  description = "投入Lambdaのbackendイメージdigest。nullではLambdaとスケジュールを作成しない。"
  validation {
    condition     = var.source_dispatch_image_digest == null || can(regex("^sha256:[0-9a-f]{64}$", var.source_dispatch_image_digest))
    error_message = "イメージはsha256 digestで指定してください。"
  }
}

variable "source_dispatch_enabled" {
  type        = bool
  default     = false
  nullable    = false
  description = "Consumer接続後に明示的に有効化する。"
  validation {
    condition     = !var.source_dispatch_enabled || var.source_dispatch_image_digest != null
    error_message = "定期投入の有効化にはイメージdigestが必要です。"
  }
}

resource "aws_sqs_queue" "source_dispatch" {
  for_each = local.source_dispatch_queue_names

  name                       = each.value
  fifo_queue                 = false
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = each.key == "acquisition" ? 1800 : 30
  redrive_policy = each.key == "acquisition" ? jsonencode({
    deadLetterTargetArn = aws_sqs_queue.acquisition_dlq.arn
    maxReceiveCount     = 5
  }) : null
}

resource "aws_sqs_queue_policy" "source_dispatch" {
  for_each  = aws_sqs_queue.source_dispatch
  queue_url = each.value.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = each.value.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
      ], each.key == "acquisition" ? [{
        Sid       = "DenySendOutsideDispatchEndpoint"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:SendMessage"
        Resource  = each.value.arn
        Condition = {
          StringNotEquals         = { "aws:sourceVpce" = aws_vpc_endpoint.outbox_sqs.id }
          StringNotEqualsIfExists = { "aws:CalledViaLast" = "sqs.amazonaws.com" }
        }
    }] : [])
  })
}

resource "aws_cloudwatch_log_group" "source_dispatch" {
  name              = "/aws/lambda/${local.source_dispatch_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "source_dispatch" {
  name                 = "${local.source_dispatch_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.source_dispatch_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "source_dispatch" {
  name = "source-dispatch"
  role = aws_iam_role.source_dispatch.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_collect"
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = [aws_sqs_queue.source_dispatch["acquisition"].arn, aws_sqs_queue.source_dispatch["execution_failure"].arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.source_dispatch.arn}:*"
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.source_dispatch_arn } }
      },
    ]
  })
}

# 投入処理はCloudWatch Logsと標準メトリクスを使い、X-Rayは採用しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "source_dispatch" {
  count = var.source_dispatch_image_digest == null ? 0 : 1

  function_name                  = local.source_dispatch_name
  role                           = aws_iam_role.source_dispatch.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.this["backend"].repository_url}@${var.source_dispatch_image_digest}"
  architectures                  = ["arm64"]
  memory_size                    = 512
  timeout                        = 120
  reserved_concurrent_executions = 3

  tracing_config {
    mode = "PassThrough"
  }
  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.source_dispatch.handler.handler"]
    working_directory = "/app"
  }
  vpc_config {
    subnet_ids         = [aws_subnet.app["api"].id]
    security_group_ids = [aws_security_group.outbox_relay.id]
  }
  environment {
    variables = {
      ENV                              = "production"
      DATABASE_URL                     = local.backend_db_url["vector_collect"]
      DB_IAM_AUTH                      = "true"
      SQS_SOURCE_ACQUISITION_QUEUE_URL = aws_sqs_queue.source_dispatch["acquisition"].url
    }
  }
  depends_on = [
    aws_iam_role_policy.source_dispatch,
    aws_cloudwatch_log_group.source_dispatch,
    aws_ecr_repository_policy.outbox_relay,
    aws_vpc_security_group_ingress_rule.rds_from_outbox_relay,
    aws_vpc_security_group_egress_rule.outbox_relay_to_rds,
    aws_vpc_security_group_ingress_rule.sqs_from_outbox_relay,
    aws_vpc_security_group_egress_rule.outbox_relay_to_sqs,
    aws_sqs_queue_policy.source_dispatch,
  ]
}

resource "aws_scheduler_schedule_group" "source_dispatch" {
  name = local.source_dispatch_name
}

resource "aws_iam_role" "source_dispatch_scheduler" {
  name                 = "${local.source_dispatch_name}-scheduler"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.source_dispatch_name}-scheduler-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.source_dispatch.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "source_dispatch_scheduler" {
  name = "invoke-source-dispatch"
  role = aws_iam_role.source_dispatch_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.source_dispatch_arn
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.source_dispatch["scheduler_failure"].arn
      },
    ]
  })
}

resource "aws_scheduler_schedule" "source_dispatch" {
  for_each = var.source_dispatch_image_digest == null ? {} : local.source_dispatch_schedules

  name                         = "${local.source_dispatch_name}-${each.key}"
  group_name                   = aws_scheduler_schedule_group.source_dispatch.name
  state                        = var.source_dispatch_enabled ? "ENABLED" : "DISABLED"
  schedule_expression          = each.value
  schedule_expression_timezone = "UTC"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.source_dispatch[0].arn
    role_arn = aws_iam_role.source_dispatch_scheduler.arn
    input    = jsonencode({ cadence = each.key, scheduled_at = "<aws.scheduler.scheduled-time>" })
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 600
    }
    dead_letter_config {
      arn = aws_sqs_queue.source_dispatch["scheduler_failure"].arn
    }
  }
  depends_on = [aws_iam_role_policy.source_dispatch_scheduler, aws_lambda_function_event_invoke_config.source_dispatch]
}

resource "aws_lambda_function_event_invoke_config" "source_dispatch" {
  count = var.source_dispatch_image_digest == null ? 0 : 1

  function_name                = aws_lambda_function.source_dispatch[0].function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 21600
  destination_config {
    on_failure {
      destination = aws_sqs_queue.source_dispatch["execution_failure"].arn
    }
  }
}

resource "aws_cloudwatch_dashboard" "source_dispatch" {
  dashboard_name = local.source_dispatch_name
  dashboard_body = jsonencode({
    widgets = [for index, key in ["scheduler_failure", "execution_failure", "consumer_failure"] : {
      type = "metric", x = (index % 2) * 12, y = floor(index / 2) * 6, width = 12, height = 6
      properties = {
        title  = key == "scheduler_failure" ? "Scheduler 配送失敗" : key == "execution_failure" ? "Lambda 実行失敗" : "取得Consumer 処理失敗"
        region = var.region, view = "timeSeries", period = 300, stat = "Maximum"
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", (key == "consumer_failure" ? aws_sqs_queue.acquisition_dlq.name : aws_sqs_queue.source_dispatch[key].name), { label = "残件数（概数）" }],
          ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", (key == "consumer_failure" ? aws_sqs_queue.acquisition_dlq.name : aws_sqs_queue.source_dispatch[key].name), { label = "最古メッセージ経過秒数", yAxis = "right" }],
        ]
      }
    }]
  })
}

output "source_dispatch" {
  value = {
    lambda_arn            = try(aws_lambda_function.source_dispatch[0].arn, null)
    acquisition_queue_url = aws_sqs_queue.source_dispatch["acquisition"].url
    failure_queues = { for key in ["scheduler_failure", "execution_failure"] : key => {
      arn = aws_sqs_queue.source_dispatch[key].arn, url = aws_sqs_queue.source_dispatch[key].url
    } }
    dashboard_url = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.source_dispatch.dashboard_name}"
  }
}
