locals {
  completion_consumer_name = "${var.name_prefix}-completion-consumer"
  completion_consumer_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.completion_consumer_name}"
}

resource "aws_sqs_queue" "completion_dlq" {
  name                      = "${var.name_prefix}-article-completion-dlq"
  fifo_queue                = false
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_redrive_allow_policy" "completion_dlq" {
  queue_url = aws_sqs_queue.completion_dlq.url
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.outbox["completion"].arn]
  })
}

resource "aws_sqs_queue_policy" "completion_dlq" {
  queue_url = aws_sqs_queue.completion_dlq.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.completion_dlq.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_security_group" "completion_consumer" {
  name        = local.completion_consumer_name
  description = "Completion consumer access to Collect RDS, article proxy and SQS only."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "completion_consumer_to_rds" {
  security_group_id            = aws_security_group.completion_consumer.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_completion_consumer" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.completion_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "completion_consumer_to_proxy" {
  security_group_id            = aws_security_group.completion_consumer.id
  referenced_security_group_id = aws_security_group.proxy.id
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_completion_consumer" {
  security_group_id            = aws_security_group.proxy.id
  referenced_security_group_id = aws_security_group.completion_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
}

resource "aws_vpc_security_group_egress_rule" "completion_consumer_to_sqs" {
  security_group_id            = aws_security_group.completion_consumer.id
  referenced_security_group_id = aws_security_group.outbox_sqs_endpoint.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "sqs_from_completion_consumer" {
  security_group_id            = aws_security_group.outbox_sqs_endpoint.id
  referenced_security_group_id = aws_security_group.completion_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_cloudwatch_log_group" "completion_consumer" {
  name              = "/aws/lambda/${local.completion_consumer_name}"
  retention_in_days = var.log_retention_days
}

# CloudWatch Logsと既存EMFを使い、X-Rayは追加しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "completion_consumer" {
  function_name                  = local.completion_consumer_name
  role                           = aws_iam_role.article_fetch.arn
  package_type                   = "Image"
  image_uri                      = local.lambda_initial_image_uri
  architectures                  = ["arm64"]
  memory_size                    = 1024
  timeout                        = 600
  reserved_concurrent_executions = 5

  tracing_config {
    mode = "PassThrough"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.completion_consumer.name
  }

  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.completion.handler.handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.completion_consumer.id]
    security_group_ids = [aws_security_group.completion_consumer.id]
  }

  environment {
    variables = {
      ENV                              = "production"
      DATABASE_URL                     = local.backend_db_url["vector_collect"]
      DB_IAM_AUTH                      = "true"
      SQS_ARTICLE_COMPLETION_QUEUE_URL = aws_sqs_queue.outbox["completion"].url
      EGRESS_PROXY_URL                 = local.proxy_url
    }
  }

  depends_on = [
    aws_iam_role_policy.article_fetch,
    aws_ecr_repository_policy.backend_lambda_pull,
    aws_route_table_association.completion_consumer,
    aws_vpc_security_group_egress_rule.completion_consumer_to_rds,
    aws_vpc_security_group_ingress_rule.rds_from_completion_consumer,
    aws_vpc_security_group_egress_rule.completion_consumer_to_proxy,
    aws_vpc_security_group_ingress_rule.proxy_from_completion_consumer,
    aws_vpc_security_group_egress_rule.completion_consumer_to_sqs,
    aws_vpc_security_group_ingress_rule.sqs_from_completion_consumer,
    # 再配信の待機はendpoint経由で可視性を変えるため、endpointが実行ロールを許可してから関数を更新する。
    aws_vpc_endpoint.outbox_sqs,
  ]

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_event_source_mapping" "completion_consumer" {
  event_source_arn                   = aws_sqs_queue.outbox["completion"].arn
  function_name                      = aws_lambda_function.completion_consumer.arn
  enabled                            = true
  batch_size                         = 10
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }

  tags       = { Consumer = local.completion_consumer_name }
  depends_on = [aws_iam_role_policy.article_fetch]
}
