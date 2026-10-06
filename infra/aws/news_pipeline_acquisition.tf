locals {
  acquisition_consumer_name = "${var.name_prefix}-acquisition-consumer"
  acquisition_consumer_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.acquisition_consumer_name}"
}

resource "aws_sqs_queue" "acquisition_dlq" {
  name                      = "${var.name_prefix}-source-acquisition-dlq"
  fifo_queue                = false
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_redrive_allow_policy" "acquisition_dlq" {
  queue_url = aws_sqs_queue.acquisition_dlq.url
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.source_dispatch["acquisition"].arn]
  })
}

resource "aws_sqs_queue_policy" "acquisition_dlq" {
  queue_url = aws_sqs_queue.acquisition_dlq.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.acquisition_dlq.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_security_group" "acquisition_consumer" {
  name        = local.acquisition_consumer_name
  description = "Acquisition consumer access to Collect RDS and article proxy only."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "acquisition_consumer_to_rds" {
  security_group_id            = aws_security_group.acquisition_consumer.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_acquisition_consumer" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.acquisition_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "acquisition_consumer_to_proxy" {
  security_group_id            = aws_security_group.acquisition_consumer.id
  referenced_security_group_id = aws_security_group.proxy.id
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_acquisition_consumer" {
  security_group_id            = aws_security_group.proxy.id
  referenced_security_group_id = aws_security_group.acquisition_consumer.id
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
}

resource "aws_cloudwatch_log_group" "acquisition_consumer" {
  name              = "/aws/lambda/${local.acquisition_consumer_name}"
  retention_in_days = var.log_retention_days
}

# CloudWatch Logsと既存EMFを使い、X-Rayは追加しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "acquisition_consumer" {
  function_name                  = local.acquisition_consumer_name
  role                           = aws_iam_role.article_fetch.arn
  package_type                   = "Image"
  image_uri                      = local.lambda_initial_image_uri
  architectures                  = ["arm64"]
  memory_size                    = 1024
  timeout                        = 300
  reserved_concurrent_executions = 5

  tracing_config {
    mode = "PassThrough"
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.acquisition_consumer.name
  }

  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.acquisition.handler.handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.acquisition_consumer.id]
    security_group_ids = [aws_security_group.acquisition_consumer.id]
  }

  environment {
    variables = {
      ENV                    = "production"
      DATABASE_URL           = local.backend_db_url["vector_collect"]
      DB_IAM_AUTH            = "true"
      CROSSREF_CONTACT_EMAIL = var.crossref_contact_email
      EGRESS_PROXY_URL       = local.proxy_url
    }
  }

  depends_on = [
    aws_iam_role_policy.article_fetch,
    aws_ecr_repository_policy.backend_lambda_pull,
    aws_route_table_association.acquisition_consumer,
    aws_vpc_security_group_egress_rule.acquisition_consumer_to_rds,
    aws_vpc_security_group_ingress_rule.rds_from_acquisition_consumer,
    aws_vpc_security_group_egress_rule.acquisition_consumer_to_proxy,
    aws_vpc_security_group_ingress_rule.proxy_from_acquisition_consumer,
  ]

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_event_source_mapping" "acquisition_consumer" {
  event_source_arn                   = aws_sqs_queue.source_dispatch["acquisition"].arn
  function_name                      = aws_lambda_function.acquisition_consumer.arn
  enabled                            = true
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }

  tags       = { Consumer = local.acquisition_consumer_name }
  depends_on = [aws_iam_role_policy.article_fetch]
}
