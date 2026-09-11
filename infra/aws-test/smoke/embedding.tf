data "aws_ecr_image" "backend" {
  repository_name = "vector-test/backend"
  image_digest    = var.backend_image_digest
}
data "aws_ecr_image" "proxy" {
  repository_name = "vector-test/proxy"
  image_digest    = var.proxy_image_digest
}
resource "aws_cloudwatch_log_group" "runtime" {
  for_each          = toset(["lambda", "runner", "proxy"])
  name              = "/vector-test/${local.prefix}/${each.key}"
  retention_in_days = 7
  tags              = local.tags
}
resource "aws_sqs_queue" "embedding" {
  name                       = "${local.prefix}-embedding"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 720
  sqs_managed_sse_enabled    = true
  tags                       = local.tags
}
resource "aws_sqs_queue_policy" "tls" {
  queue_url = aws_sqs_queue.embedding.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.embedding.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}
resource "aws_lambda_function" "embedding" {
  function_name = "${local.prefix}-embedding"
  role          = aws_iam_role.runtime["lambda"].arn
  package_type  = "Image"
  image_uri     = local.images.backend
  architectures = ["arm64"]
  memory_size   = 1024
  timeout       = 120
  image_config {
    entry_point       = ["/app/.venv/bin/python", "-m", "awslambdaric"]
    command           = ["app.lambda_handlers.embedding.handler"]
    working_directory = "/app"
  }
  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.runtime["lambda"].name
  }
  vpc_config {
    subnet_ids         = [aws_subnet.smoke["lambda"].id]
    security_group_ids = [aws_security_group.smoke["lambda"].id]
  }
  environment {
    variables = {
      ENV                           = "production"
      DATABASE_URL                  = "postgresql+asyncpg://vector_app@${aws_db_instance.smoke.address}:5432/vector?sslmode=require"
      DB_IAM_AUTH                   = "true"
      GEMINI_API_KEY_PARAMETER_PATH = var.gemini_parameter_path
      EGRESS_PROXY_URL              = local.proxy_url
    }
  }
  # 関数削除後にENIの消滅確認を挟み、実行権限の削除を待たせる。
  depends_on = [
    terraform_data.lambda_eni_cleanup,
    aws_iam_role_policy.runtime,
    aws_vpc_security_group_egress_rule.private,
    aws_vpc_security_group_ingress_rule.private,
    aws_route_table_association.smoke,
    aws_vpc_endpoint.ssm,
    data.aws_ecr_image.backend,
  ]
  tags = local.tags
}
resource "aws_lambda_event_source_mapping" "embedding" {
  event_source_arn                   = aws_sqs_queue.embedding.arn
  function_name                      = aws_lambda_function.embedding.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]
  enabled                            = true
  scaling_config { maximum_concurrency = 2 }
  tags = local.tags
}
