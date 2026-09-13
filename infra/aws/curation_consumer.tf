locals {
  curation_consumer_name           = "${var.name_prefix}-curation-consumer"
  curation_consumer_arn            = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.curation_consumer_name}"
  curation_consumer_parameter_path = "/${var.name_prefix}/curation-consumer/gemini-api-key"
  curation_consumer_subnet_cidr    = cidrsubnet(var.vpc_cidr, 8, 32)
  curation_consumer_eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
}

resource "aws_sqs_queue" "curation_dlq" {
  name                      = "${var.name_prefix}-article-curation-dlq"
  fifo_queue                = false
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_redrive_allow_policy" "curation_dlq" {
  queue_url = aws_sqs_queue.curation_dlq.url
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.outbox["curation"].arn]
  })
}

resource "aws_sqs_queue_policy" "curation_dlq" {
  queue_url = aws_sqs_queue.curation_dlq.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.curation_dlq.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_subnet" "curation_consumer" {
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.az_primary
  cidr_block              = local.curation_consumer_subnet_cidr
  map_public_ip_on_launch = false
  tags                    = { Name = local.curation_consumer_name }
}

resource "aws_route_table_association" "curation_consumer" {
  subnet_id      = aws_subnet.curation_consumer.id
  route_table_id = aws_route_table.app.id
}

resource "aws_security_group" "curation_consumer" {
  name        = local.curation_consumer_name
  description = "Curation consumer access to RDS, Gemini proxy and SSM only."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "curation_consumer_ssm" {
  name        = "${local.curation_consumer_name}-ssm"
  description = "SSM endpoint access from the curation consumer."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "curation_consumer_to_rds" {
  security_group_id            = aws_security_group.curation_consumer.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_curation_consumer" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.curation_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "curation_consumer_to_proxy" {
  security_group_id            = aws_security_group.curation_consumer.id
  referenced_security_group_id = aws_security_group.proxy.id
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_curation_consumer" {
  security_group_id            = aws_security_group.proxy.id
  referenced_security_group_id = aws_security_group.curation_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
}

resource "aws_vpc_security_group_egress_rule" "curation_consumer_to_ssm" {
  security_group_id            = aws_security_group.curation_consumer.id
  referenced_security_group_id = aws_security_group.curation_consumer_ssm.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "ssm_from_curation_consumer" {
  security_group_id            = aws_security_group.curation_consumer_ssm.id
  referenced_security_group_id = aws_security_group.curation_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_cloudwatch_log_group" "curation_consumer" {
  name              = "/aws/lambda/${local.curation_consumer_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "curation_consumer" {
  name                 = "${local.curation_consumer_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.curation_consumer_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "curation_consumer" {
  name = "curation-consumer"
  role = aws_iam_role.curation_consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeCurationEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.outbox["curation"].arn
      },
      {
        Sid      = "RdsIamAuthAsApp"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_app"
      },
      {
        Sid      = "ReadGeminiKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.curation_consumer_parameter_path}"
      },
      {
        Sid      = "WriteConsumerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.curation_consumer.arn}:*"
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.curation_consumer_arn } }
      },
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "curation_dlq_not_empty" {
  alarm_name          = "${local.curation_consumer_name}-dlq-not-empty"
  alarm_description   = "CurationのDLQに未対応メッセージがある。原因を確認し、必要ならSQSトリガーを手動停止する。自動停止・再投入は行わない。"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.curation_dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# CloudWatch Logsと既存EMFを使い、X-Rayは追加しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "curation_consumer" {
  count = var.curation_consumer_image_digest == null ? 0 : 1

  function_name                  = local.curation_consumer_name
  role                           = aws_iam_role.curation_consumer.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.this["backend"].repository_url}@${var.curation_consumer_image_digest}"
  architectures                  = ["arm64"]
  memory_size                    = 1024
  timeout                        = 120
  reserved_concurrent_executions = 10

  tracing_config {
    mode = "PassThrough"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.curation_consumer.name
  }

  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.curation.handler.handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.curation_consumer.id]
    security_group_ids = [aws_security_group.curation_consumer.id]
  }

  environment {
    variables = {
      ENV                           = "production"
      DATABASE_URL                  = local.backend_db_url["vector_app"]
      DB_IAM_AUTH                   = "true"
      GEMINI_API_KEY_PARAMETER_PATH = local.curation_consumer_parameter_path
      EGRESS_PROXY_URL              = local.proxy_url
    }
  }

  depends_on = [
    aws_iam_role_policy.curation_consumer,
    aws_ecr_repository_policy.outbox_relay,
    aws_route_table_association.curation_consumer,
    aws_vpc_security_group_egress_rule.curation_consumer_to_rds,
    aws_vpc_security_group_ingress_rule.rds_from_curation_consumer,
    aws_vpc_security_group_egress_rule.curation_consumer_to_proxy,
    aws_vpc_security_group_ingress_rule.proxy_from_curation_consumer,
    aws_vpc_security_group_egress_rule.curation_consumer_to_ssm,
    aws_vpc_security_group_ingress_rule.ssm_from_curation_consumer,
  ]
}

resource "aws_lambda_event_source_mapping" "curation_consumer" {
  count = var.curation_consumer_image_digest == null ? 0 : 1

  event_source_arn                   = aws_sqs_queue.outbox["curation"].arn
  function_name                      = aws_lambda_function.curation_consumer[0].arn
  enabled                            = true
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 10
  }

  tags       = { Consumer = local.curation_consumer_name }
  depends_on = [aws_iam_role_policy.curation_consumer]
}
