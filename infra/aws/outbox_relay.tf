locals {
  outbox_relay_name = "${var.name_prefix}-outbox-relay"
  outbox_relay_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.outbox_relay_name}"
  outbox_queue_stages = toset([
    "completion", "curation", "assessment", "embedding",
  ])
  outbox_relay_eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
}

resource "aws_sqs_queue" "outbox" {
  for_each = local.outbox_queue_stages

  name                       = "${var.name_prefix}-article-${each.key}"
  fifo_queue                 = false
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = contains(["embedding", "assessment", "curation"], each.key) ? 345600 : 1209600
  visibility_timeout_seconds = contains(["embedding", "assessment", "curation"], each.key) ? 720 : 30
  redrive_policy = contains(["embedding", "assessment", "curation"], each.key) ? jsonencode({
    deadLetterTargetArn = { embedding = aws_sqs_queue.embedding_dlq.arn, assessment = aws_sqs_queue.assessment_dlq.arn, curation = aws_sqs_queue.curation_dlq.arn }[each.key]
    maxReceiveCount     = 5
  }) : null
}

resource "aws_security_group" "outbox_relay" {
  name        = local.outbox_relay_name
  description = "Outbox relay Lambda access to RDS and SQS only."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "outbox_sqs_endpoint" {
  name        = "${var.name_prefix}-outbox-sqs-vpce"
  description = "SQS private endpoint for the outbox relay."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_outbox_relay" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.outbox_relay.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "outbox_relay_to_rds" {
  security_group_id            = aws_security_group.outbox_relay.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "sqs_from_outbox_relay" {
  security_group_id            = aws_security_group.outbox_sqs_endpoint.id
  referenced_security_group_id = aws_security_group.outbox_relay.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "outbox_relay_to_sqs" {
  security_group_id            = aws_security_group.outbox_relay.id
  referenced_security_group_id = aws_security_group.outbox_sqs_endpoint.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "outbox_sqs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app["api"].id]
  security_group_ids  = [aws_security_group.outbox_sqs_endpoint.id]
  private_dns_enabled = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.outbox_relay.arn }
        Action    = "sqs:SendMessage"
        Resource  = [for queue in aws_sqs_queue.outbox : queue.arn]
      },
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.assessment_outbox_relay.arn }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.outbox["assessment"].arn
      },
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.curation_outbox_relay.arn }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.outbox["curation"].arn
      },
    ]
  })
  tags = { Name = "${var.name_prefix}-vpce-sqs" }
}

resource "aws_sqs_queue_policy" "outbox" {
  for_each  = aws_sqs_queue.outbox
  queue_url = each.value.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = each.value.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenySendOutsideRelayEndpoint"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:SendMessage"
        Resource  = each.value.arn
        Condition = { StringNotEquals = { "aws:sourceVpce" = aws_vpc_endpoint.outbox_sqs.id } }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "outbox_relay" {
  name              = "/aws/lambda/${local.outbox_relay_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "outbox_relay" {
  name                 = "${local.outbox_relay_name}-lambda"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.outbox_relay_name}-lambda-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "outbox_relay" {
  name = "outbox-relay"
  role = aws_iam_role.outbox_relay.id
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
        Resource = [for queue in aws_sqs_queue.outbox : queue.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.outbox_relay.arn}:*"
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
        Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.outbox_relay_arn } }
      },
    ]
  })
}

resource "aws_ecr_repository_policy" "outbox_relay" {
  repository = aws_ecr_repository.this["backend"].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaImageRetrieval"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = {
        ArnLike      = { "aws:SourceArn" = [local.outbox_relay_arn, local.embedding_consumer_arn, local.assessment_outbox_relay_arn, local.assessment_consumer_arn, local.curation_consumer_arn, local.curation_outbox_relay_arn] }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

# relayはCloudWatch Logsと標準メトリクスを使い、X-Rayは採用しない。
# nosemgrep: terraform.aws.security.aws-lambda-x-ray-tracing-not-active.aws-lambda-x-ray-tracing-not-active
resource "aws_lambda_function" "outbox_relay" {
  count = var.outbox_relay_image_digest == null ? 0 : 1

  function_name                  = local.outbox_relay_name
  role                           = aws_iam_role.outbox_relay.arn
  package_type                   = "Image"
  image_uri                      = "${aws_ecr_repository.this["backend"].repository_url}@${var.outbox_relay_image_digest}"
  architectures                  = ["arm64"]
  memory_size                    = 512
  timeout                        = 120
  reserved_concurrent_executions = 1

  tracing_config {
    mode = "PassThrough"
  }

  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.outbox_relay.handler"]
    working_directory = "/app"
  }

  vpc_config {
    subnet_ids         = [aws_subnet.app["api"].id]
    security_group_ids = [aws_security_group.outbox_relay.id]
  }

  environment {
    variables = merge({
      ENV          = "production"
      DATABASE_URL = local.backend_db_url["vector_app"]
      DB_IAM_AUTH  = "true"
      }, {
      for stage, queue in aws_sqs_queue.outbox :
      "SQS_ARTICLE_${upper(stage)}_QUEUE_URL" => queue.url
    })
  }

  depends_on = [
    aws_iam_role_policy.outbox_relay,
    aws_ecr_repository_policy.outbox_relay,
    aws_vpc_security_group_ingress_rule.rds_from_outbox_relay,
    aws_vpc_security_group_egress_rule.outbox_relay_to_rds,
  ]
}

resource "aws_scheduler_schedule_group" "outbox_relay" {
  name = local.outbox_relay_name
}

resource "aws_iam_role" "outbox_relay_scheduler" {
  name                 = "${local.outbox_relay_name}-scheduler"
  path                 = "/${var.name_prefix}/"
  permissions_boundary = "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/${local.outbox_relay_name}-scheduler-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.outbox_relay.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "outbox_relay_scheduler" {
  name = "invoke-outbox-relay"
  role = aws_iam_role.outbox_relay_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = local.outbox_relay_arn
    }]
  })
}

resource "aws_scheduler_schedule" "outbox_relay" {
  count = var.outbox_relay_image_digest == null ? 0 : 1

  name                = local.outbox_relay_name
  group_name          = aws_scheduler_schedule_group.outbox_relay.name
  state               = "ENABLED"
  schedule_expression = "rate(1 minute)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.outbox_relay[0].arn
    role_arn = aws_iam_role.outbox_relay_scheduler.arn
    input    = "{}"
  }

  depends_on = [aws_iam_role_policy.outbox_relay_scheduler]
}
