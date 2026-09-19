locals {
  acquisition_consumer_name        = "${var.name_prefix}-acquisition-consumer"
  acquisition_consumer_arn         = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.acquisition_consumer_name}"
  acquisition_consumer_subnet_cidr = cidrsubnet(var.vpc_cidr, 8, 34)
  acquisition_consumer_eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
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

resource "aws_subnet" "acquisition_consumer" {
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.az_primary
  cidr_block              = local.acquisition_consumer_subnet_cidr
  map_public_ip_on_launch = false
  tags                    = { Name = local.acquisition_consumer_name }
}

resource "aws_route_table_association" "acquisition_consumer" {
  subnet_id      = aws_subnet.acquisition_consumer.id
  route_table_id = aws_route_table.app.id
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

resource "aws_iam_role" "acquisition_consumer" {
  name                 = "${local.acquisition_consumer_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.acquisition_consumer_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "acquisition_consumer" {
  name = "acquisition-consumer"
  role = aws_iam_role.acquisition_consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConsumeAcquisitionEvents"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.source_dispatch["acquisition"].arn
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
        Resource = "${aws_cloudwatch_log_group.acquisition_consumer.arn}:*"
      },
      {
        Sid      = "ManageLambdaNetworkInterfaces"
        Effect   = "Allow"
        Action   = local.acquisition_consumer_eni_actions
        Resource = "*"
      },
      {
        Sid       = "DenyEniOperationsFromFunctionCode"
        Effect    = "Deny"
        Action    = local.acquisition_consumer_eni_actions
        Resource  = "*"
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.acquisition_consumer_arn } }
      },
    ]
  })
}

# CloudWatch Logsと既存EMFを使い、X-Rayは追加しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "acquisition_consumer" {
  count = var.acquisition_consumer_image_digest == null ? 0 : 1

  function_name                  = local.acquisition_consumer_name
  role                           = aws_iam_role.acquisition_consumer.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.this["backend"].repository_url}@${var.acquisition_consumer_image_digest}"
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
    aws_iam_role_policy.acquisition_consumer,
    aws_ecr_repository_policy.backend_lambda_pull,
    aws_route_table_association.acquisition_consumer,
    aws_vpc_security_group_egress_rule.acquisition_consumer_to_rds,
    aws_vpc_security_group_ingress_rule.rds_from_acquisition_consumer,
    aws_vpc_security_group_egress_rule.acquisition_consumer_to_proxy,
    aws_vpc_security_group_ingress_rule.proxy_from_acquisition_consumer,
  ]
}

resource "aws_lambda_event_source_mapping" "acquisition_consumer" {
  count = var.acquisition_consumer_image_digest == null ? 0 : 1

  event_source_arn                   = aws_sqs_queue.source_dispatch["acquisition"].arn
  function_name                      = aws_lambda_function.acquisition_consumer[0].arn
  enabled                            = var.acquisition_consumer_enabled
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }

  tags       = { Consumer = local.acquisition_consumer_name }
  depends_on = [aws_iam_role_policy.acquisition_consumer]
}

variable "acquisition_consumer_image_digest" {
  type        = string
  default     = null
  description = "取得Consumerのbackendイメージdigest。nullでは関数と受信接続を作成しない。"
  validation {
    condition     = var.acquisition_consumer_image_digest == null || can(regex("^sha256:[0-9a-f]{64}$", var.acquisition_consumer_image_digest))
    error_message = "取得Consumerはsha256 digestで指定してください。"
  }
}

variable "acquisition_consumer_enabled" {
  type        = bool
  default     = false
  nullable    = false
  description = "取得Consumerの受信を明示的に開始する。"
  validation {
    condition     = !var.acquisition_consumer_enabled || var.acquisition_consumer_image_digest != null
    error_message = "受信の有効化にはイメージdigestが必要です。"
  }
}

output "acquisition_consumer" {
  value = {
    lambda_arn                = try(aws_lambda_function.acquisition_consumer[0].arn, null)
    event_source_mapping_uuid = try(aws_lambda_event_source_mapping.acquisition_consumer[0].uuid, null)
    enabled                   = var.acquisition_consumer_enabled
    image_digest              = var.acquisition_consumer_image_digest
    dlq_url                   = aws_sqs_queue.acquisition_dlq.url
    dlq_arn                   = aws_sqs_queue.acquisition_dlq.arn
    log_group                 = aws_cloudwatch_log_group.acquisition_consumer.name
    dashboard_url             = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.source_dispatch.dashboard_name}"
  }
}
