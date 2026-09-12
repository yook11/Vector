output "run" {
  value = {
    account_id           = var.expected_account_id, region = local.region, run_id = var.run_id
    source_revision      = var.source_revision
    backend_image        = local.images.backend, proxy_image = local.images.proxy
    backend_image_digest = var.backend_image_digest, proxy_image_digest = var.proxy_image_digest
    ami_id               = nonsensitive(data.aws_ssm_parameter.amazon_linux.value)
    state_bucket         = "vector-test-tfstate-${var.expected_account_id}"
    state_key            = "smoke/${var.run_id}/terraform.tfstate"
  }
}
output "database" {
  value = {
    address           = aws_db_instance.smoke.address, port = aws_db_instance.smoke.port
    name              = aws_db_instance.smoke.db_name, identifier = aws_db_instance.smoke.identifier
    resource_id       = aws_db_instance.smoke.resource_id
    master_secret_arn = aws_db_instance.smoke.master_user_secret[0].secret_arn
    migration_role    = "vector", application_role = "vector_app"
  }
}
output "execution" {
  value = {
    instance_ids              = { for k, v in aws_instance.runtime : k => v.id }
    queue_url                 = aws_sqs_queue.embedding.url, queue_arn = aws_sqs_queue.embedding.arn
    lambda_name               = aws_lambda_function.embedding.function_name
    event_source_mapping_uuid = aws_lambda_event_source_mapping.embedding.uuid
    log_groups                = merge({ for k, v in aws_cloudwatch_log_group.runtime : k => v.name }, { database = aws_cloudwatch_log_group.database.name })
    bootstrap_status_path     = "/var/lib/vector-test/bootstrap-status.json"
  }
}
output "resources" {
  description = "後続の削除確認がstate削除前に保存するID一覧（AWS生成のENIはVPC内検索も必要）。"
  value = {
    vpc                  = aws_vpc.smoke.id
    subnets              = { for k, v in aws_subnet.smoke : k => v.id }
    route_tables         = { for k, v in aws_route_table.smoke : k => v.id }
    internet_gateway     = aws_internet_gateway.proxy.id
    security_groups      = { for k, v in aws_security_group.smoke : k => v.id }
    ssm_endpoint         = aws_vpc_endpoint.ssm.id
    ssmmessages_endpoint = aws_vpc_endpoint.ssmmessages.id
    instances            = { for k, v in aws_instance.runtime : k => v.id }
    root_volumes         = { for k, v in aws_instance.runtime : k => v.root_block_device[0].volume_id }
    proxy_public_ip      = aws_instance.runtime["proxy"].public_ip
    rds                  = aws_db_instance.smoke.identifier
    db_subnet_group      = aws_db_subnet_group.smoke.name
    db_parameter_group   = aws_db_parameter_group.smoke.name
    master_secret_arn    = aws_db_instance.smoke.master_user_secret[0].secret_arn
    queue                = aws_sqs_queue.embedding.url
    lambda               = aws_lambda_function.embedding.function_name
    event_source_mapping = aws_lambda_event_source_mapping.embedding.uuid
    roles                = { for k, v in aws_iam_role.runtime : k => v.name }
    instance_profiles    = { for k, v in aws_iam_instance_profile.runtime : k => v.name }
    log_groups           = merge({ for k, v in aws_cloudwatch_log_group.runtime : k => v.name }, { database = aws_cloudwatch_log_group.database.name })
  }
}
