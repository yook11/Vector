locals {
  completion_consumer_name        = "${var.name_prefix}-completion-consumer"
  completion_consumer_arn         = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.completion_consumer_name}"
  completion_consumer_subnet_cidr = cidrsubnet(var.vpc_cidr, 8, 33)
  completion_consumer_eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
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

resource "aws_subnet" "completion_consumer" {
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.az_primary
  cidr_block              = local.completion_consumer_subnet_cidr
  map_public_ip_on_launch = false
  tags                    = { Name = local.completion_consumer_name }
}

resource "aws_route_table_association" "completion_consumer" {
  subnet_id      = aws_subnet.completion_consumer.id
  route_table_id = aws_route_table.app.id
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

resource "aws_iam_role" "completion_consumer" {
  name                 = "${local.completion_consumer_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.completion_consumer_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "completion_consumer" {
  name = "completion-consumer"
  role = aws_iam_role.completion_consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeCompletionEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = aws_sqs_queue.outbox["completion"].arn
      },
      {
        Sid      = "RdsIamAuthAsCollect"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector_collect"
      },
      {
        Sid      = "WriteConsumerLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.completion_consumer.arn}:*"
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.completion_consumer_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyEniOperationsFromFunctionCode"
        Effect    = "Deny"
        Action    = local.completion_consumer_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.completion_consumer_arn } }
      },
    ]
  })
}

# CloudWatch Logsと既存EMFを使い、X-Rayは追加しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "completion_consumer" {
  count = var.completion_consumer_image_digest == null ? 0 : 1

  function_name                  = local.completion_consumer_name
  role                           = aws_iam_role.completion_consumer.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.this["backend"].repository_url}@${var.completion_consumer_image_digest}"
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
    aws_iam_role_policy.completion_consumer,
    aws_ecr_repository_policy.backend_lambda_pull,
    aws_route_table_association.completion_consumer,
    aws_vpc_security_group_egress_rule.completion_consumer_to_rds,
    aws_vpc_security_group_ingress_rule.rds_from_completion_consumer,
    aws_vpc_security_group_egress_rule.completion_consumer_to_proxy,
    aws_vpc_security_group_ingress_rule.proxy_from_completion_consumer,
    aws_vpc_security_group_egress_rule.completion_consumer_to_sqs,
    aws_vpc_security_group_ingress_rule.sqs_from_completion_consumer,
  ]
}

resource "aws_lambda_event_source_mapping" "completion_consumer" {
  count = var.completion_consumer_image_digest == null ? 0 : 1

  event_source_arn                   = aws_sqs_queue.outbox["completion"].arn
  function_name                      = aws_lambda_function.completion_consumer[0].arn
  enabled                            = var.completion_consumer_enabled
  batch_size                         = 10
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }

  tags       = { Consumer = local.completion_consumer_name }
  depends_on = [aws_iam_role_policy.completion_consumer]
}
