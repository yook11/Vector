output "vpc_id" {
  value = aws_vpc.main.id
}

output "app_subnet_ids" {
  description = "段名 -> subnet ID。ECS service の network configuration が使う。"
  value       = { for name, subnet in aws_subnet.app : name => subnet.id }
}

output "app_security_group_ids" {
  description = "段名 -> security group ID。"
  value       = { for name, sg in aws_security_group.app : name => sg.id }
}

output "alb_subnet_ids" {
  value = [for subnet in aws_subnet.public_alb : subnet.id]
}

output "data_subnet_ids" {
  description = "RDS subnet group / ElastiCache subnet group 用。"
  value       = [for subnet in aws_subnet.data : subnet.id]
}

output "proxy_subnet_id" {
  value = aws_subnet.proxy.id
}

output "migration_subnet_id" {
  value = aws_subnet.migration.id
}

output "migration_security_group_id" {
  value = aws_security_group.migration.id
}

output "migration_base_task_definition_arn" {
  value = aws_ecs_task_definition.migration_base.arn
}

output "egress_public_ip" {
  description = "外向き通信の送信元 IP。外部ベンダーの allowlist 登録に使う。"
  value       = aws_eip.nat.public_ip
}

output "task_role_arns" {
  description = "段名 -> task role ARN。ECS task definition の taskRoleArn。"
  value       = { for name, role in aws_iam_role.task : name => role.arn }
}

output "execution_role_arns" {
  description = "段名 -> execution role ARN。ECS task definition の executionRoleArn。"
  value       = { for name, role in aws_iam_role.execution : name => role.arn }
}

output "migration_role_arns" {
  value = {
    task      = aws_iam_role.migration_task.arn
    execution = aws_iam_role.migration_execution.arn
  }
}

output "ecr_repository_urls" {
  value = { for name, repo in aws_ecr_repository.this : name => repo.repository_url }
}

output "bastion_instance_id" {
  description = "DB 踏み台の instance ID (enable_db_bastion=false のときは null)。"
  value       = one(aws_instance.bastion[*].id)
}

output "db_endpoint" {
  description = "RDS の endpoint。踏み台の port forward と verify-full の host= に使う。"
  value       = aws_db_instance.this.address
}

output "parameter_store_paths" {
  description = <<-EOT
    段ごとに実値を投入する path。Terraform は作らない。
    aws ssm put-parameter --type SecureString --name /vector/<段>/<key> --value ...
  EOT
  value       = { for name, _ in local.stages : name => "/${var.name_prefix}/${name}/" }
}

output "outbox_queue_urls" {
  description = "工程名から送信先Queue URLへの対応。"
  value       = { for stage, queue in aws_sqs_queue.outbox : stage => queue.url }
}

output "outbox_queue_arns" {
  description = "工程名から送信先Queue ARNへの対応。"
  value       = { for stage, queue in aws_sqs_queue.outbox : stage => queue.arn }
}

output "outbox_relay_image_digest" {
  description = "通常plan/applyが保持するrelayのデプロイ済みイメージdigest。"
  value       = var.outbox_relay_image_digest
}

output "outbox_relay_function_name" {
  description = "イメージ未指定時はnull。"
  value       = one(aws_lambda_function.outbox_relay[*].function_name)
}

output "embedding_consumer_subnet_id" {
  value = aws_subnet.embedding_consumer.id
}

output "embedding_consumer_security_group_id" {
  value = aws_security_group.embedding_consumer.id
}

output "embedding_consumer_role_arn" {
  value = aws_iam_role.embedding_consumer.arn
}

output "embedding_consumer_log_group_name" {
  value = aws_cloudwatch_log_group.embedding_consumer.name
}

output "embedding_consumer_parameter_path" {
  value = local.embedding_consumer_parameter_path
}

output "embedding_dlq_url" {
  value = aws_sqs_queue.embedding_dlq.url
}

output "embedding_dlq_arn" {
  value = aws_sqs_queue.embedding_dlq.arn
}

output "embedding_consumer_function_name" {
  value = try(aws_lambda_function.embedding_consumer[0].function_name, null)
}

output "embedding_consumer_function_arn" {
  value = try(aws_lambda_function.embedding_consumer[0].arn, null)
}

output "embedding_consumer_image_digest" {
  value = var.embedding_consumer_image_digest
}

output "embedding_consumer_event_source_mapping_uuid" {
  value = try(aws_lambda_event_source_mapping.embedding_consumer[0].uuid, null)
}

output "assessment_consumer_subnet_id" {
  value = aws_subnet.assessment_consumer.id
}

output "assessment_consumer_security_group_id" {
  value = aws_security_group.assessment_consumer.id
}

output "assessment_consumer_role_arn" {
  value = aws_iam_role.assessment_consumer.arn
}

output "assessment_consumer_log_group_name" {
  value = aws_cloudwatch_log_group.assessment_consumer.name
}

output "assessment_consumer_parameter_path" {
  value = local.assessment_consumer_parameter_path
}

output "assessment_dlq_url" {
  value = aws_sqs_queue.assessment_dlq.url
}

output "assessment_dlq_arn" {
  value = aws_sqs_queue.assessment_dlq.arn
}

output "assessment_consumer_function_name" {
  value = try(aws_lambda_function.assessment_consumer[0].function_name, null)
}

output "assessment_consumer_function_arn" {
  value = try(aws_lambda_function.assessment_consumer[0].arn, null)
}

output "assessment_consumer_image_digest" {
  value = var.assessment_consumer_image_digest
}

output "assessment_consumer_event_source_mapping_uuid" {
  value = try(aws_lambda_event_source_mapping.assessment_consumer[0].uuid, null)
}

output "assessment_outbox_relay_function_name" {
  value = one(aws_lambda_function.assessment_outbox_relay[*].function_name)
}

output "assessment_outbox_relay_image_digest" {
  value = var.assessment_outbox_relay_image_digest
}
