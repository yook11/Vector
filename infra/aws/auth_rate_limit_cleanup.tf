locals {
  auth_rate_limit_cleanup_name = "${var.name_prefix}-auth-rate-limit-cleanup"
  auth_rate_limit_cleanup_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.auth_rate_limit_cleanup_name}"
}

resource "aws_cloudwatch_log_group" "auth_rate_limit_cleanup" {
  name              = "/aws/lambda/${local.auth_rate_limit_cleanup_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "auth_rate_limit_cleanup" {
  name                 = "${local.auth_rate_limit_cleanup_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.auth_rate_limit_cleanup_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "auth_rate_limit_cleanup" {
  name = "auth-rate-limit-cleanup"
  role = aws_iam_role.auth_rate_limit_cleanup.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_auth_rate_limit_cleanup"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.auth_rate_limit_cleanup.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.auth_rate_limit_cleanup_arn } }
      },
    ]
  })
}

# 認証カウンター掃除はCloudWatch Logsと標準メトリクスを使用する。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "auth_rate_limit_cleanup" {
  count = var.auth_rate_limit_cleanup_image_digest == null ? 0 : 1

  function_name                  = local.auth_rate_limit_cleanup_name
  role                           = aws_iam_role.auth_rate_limit_cleanup.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.this["backend"].repository_url}@${var.auth_rate_limit_cleanup_image_digest}"
  architectures                  = ["arm64"]
  memory_size                    = 512
  timeout                        = 30
  reserved_concurrent_executions = 1

  tracing_config { mode = "PassThrough" }

  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.auth_rate_limit_cleanup.handler.handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.app["api"].id]
    security_group_ids = [aws_security_group.outbox_relay.id]
  }

  environment {
    variables = {
      ENV          = "production"
      DATABASE_URL = local.backend_db_url["vector_auth_rate_limit_cleanup"]
      DB_IAM_AUTH  = "true"
    }
  }

  depends_on = [
    aws_iam_role_policy.auth_rate_limit_cleanup,
    aws_ecr_repository_policy.outbox_relay,
    aws_vpc_security_group_ingress_rule.rds_from_outbox_relay,
    aws_vpc_security_group_egress_rule.outbox_relay_to_rds,
  ]
}

resource "aws_lambda_function_event_invoke_config" "auth_rate_limit_cleanup" {
  count = var.auth_rate_limit_cleanup_image_digest == null ? 0 : 1

  function_name                = aws_lambda_function.auth_rate_limit_cleanup[0].function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 600

  destination_config {
    on_failure { destination = aws_sns_topic.alerts.arn }
  }
}

resource "aws_scheduler_schedule_group" "auth_rate_limit_cleanup" {
  name = local.auth_rate_limit_cleanup_name
}

resource "aws_iam_role" "auth_rate_limit_cleanup_scheduler" {
  name                 = "${local.auth_rate_limit_cleanup_name}-scheduler"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.auth_rate_limit_cleanup_name}-scheduler-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.auth_rate_limit_cleanup.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "auth_rate_limit_cleanup_scheduler" {
  name = "invoke-auth-rate-limit-cleanup"
  role = aws_iam_role.auth_rate_limit_cleanup_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = local.auth_rate_limit_cleanup_arn
    }]
  })
}

resource "aws_scheduler_schedule" "auth_rate_limit_cleanup" {
  count = var.auth_rate_limit_cleanup_image_digest == null ? 0 : 1

  name                         = local.auth_rate_limit_cleanup_name
  group_name                   = aws_scheduler_schedule_group.auth_rate_limit_cleanup.name
  state                        = var.auth_rate_limit_cleanup_enabled ? "ENABLED" : "DISABLED"
  schedule_expression          = "cron(20,50 * * * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.auth_rate_limit_cleanup[0].arn
    role_arn = aws_iam_role.auth_rate_limit_cleanup_scheduler.arn
    input    = "{}"

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }

  depends_on = [aws_iam_role_policy.auth_rate_limit_cleanup_scheduler, aws_lambda_function_event_invoke_config.auth_rate_limit_cleanup]
}

locals {
  auth_rate_limit_cleanup_alarms = {
    async_events_dropped = { namespace = "AWS/Lambda", metric = "AsyncEventsDropped", dimension = "FunctionName" }
    destination_failure  = { namespace = "AWS/Lambda", metric = "DestinationDeliveryFailures", dimension = "FunctionName" }
    scheduler_failure    = { namespace = "AWS/Scheduler", metric = "TargetErrorCount", dimension = "ScheduleGroup" }
  }
}

resource "aws_cloudwatch_metric_alarm" "auth_rate_limit_cleanup" {
  for_each = local.auth_rate_limit_cleanup_alarms

  alarm_name          = "${local.auth_rate_limit_cleanup_name}-${replace(each.key, "_", "-")}"
  alarm_description   = "認証カウンター掃除の${each.key}を検知した。LambdaとSchedulerのログを確認する。"
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { (each.value.dimension) = local.auth_rate_limit_cleanup_name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
