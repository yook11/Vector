output "vpc_id" {
  value = aws_vpc.main.id
}

output "app_subnet_ids" {
  description = "サービス名 -> subnet ID。ECS service の network configuration が使う。"
  value       = { for name, subnet in aws_subnet.app : name => subnet.id }
}

output "app_security_group_ids" {
  description = "サービス名 -> security group ID。"
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
  description = "サービス名 -> task role ARN。ECS task definition の taskRoleArn。"
  value       = { for name, role in aws_iam_role.task : name => role.arn }
}

output "execution_role_arns" {
  description = "サービス名 -> execution role ARN。ECS task definition の executionRoleArn。"
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

output "db_roles_network" {
  value = {
    subnet_id           = aws_subnet.migration.id
    security_group_id   = aws_security_group.db_roles.id
    secrets_endpoint_id = aws_vpc_endpoint.db_roles_secrets.id
  }
}

output "parameter_store_paths" {
  description = <<-EOT
    サービスごとに実値を投入する path。Terraform は作らない。
    aws ssm put-parameter --type SecureString --name /vector/<サービス>/<key> --value ...
  EOT
  value       = { for name, _ in local.services : name => "/${var.name_prefix}/${name}/" }
}

output "source_dispatch" {
  value = {
    lambda_arn            = aws_lambda_function.source_dispatch.arn
    acquisition_queue_url = aws_sqs_queue.source_dispatch["acquisition"].url
    failure_queues = { for key in ["scheduler_failure", "execution_failure"] : key => {
      arn = aws_sqs_queue.source_dispatch[key].arn, url = aws_sqs_queue.source_dispatch[key].url
    } }
    dashboard_url = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.source_dispatch.dashboard_name}"
  }
}

output "acquisition_consumer" {
  value = {
    lambda_arn                = aws_lambda_function.acquisition_consumer.arn
    event_source_mapping_uuid = aws_lambda_event_source_mapping.acquisition_consumer.uuid
    dlq_url                   = aws_sqs_queue.acquisition_dlq.url
    dlq_arn                   = aws_sqs_queue.acquisition_dlq.arn
    log_group                 = aws_cloudwatch_log_group.acquisition_consumer.name
    dashboard_url             = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.source_dispatch.dashboard_name}"
  }
}

output "outbox_queue_urls" {
  description = "工程名から送信先Queue URLへの対応。"
  value       = { for stage, queue in aws_sqs_queue.outbox : stage => queue.url }
}

output "outbox_queue_arns" {
  description = "工程名から送信先Queue ARNへの対応。"
  value       = { for stage, queue in aws_sqs_queue.outbox : stage => queue.arn }
}

output "outbox_relay_function_name" {
  description = "イメージ未指定時はnull。"
  value       = aws_lambda_function.outbox_relay.function_name
}

output "embedding_consumer_subnet_id" {
  value = aws_subnet.embedding_consumer.id
}

output "embedding_consumer_security_group_id" {
  value = aws_security_group.embedding_consumer.id
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
  value = aws_lambda_function.embedding_consumer.function_name
}

output "embedding_consumer_function_arn" {
  value = aws_lambda_function.embedding_consumer.arn
}

output "embedding_consumer_event_source_mapping_uuid" {
  value = aws_lambda_event_source_mapping.embedding_consumer.uuid
}

output "assessment_consumer_subnet_id" {
  value = aws_subnet.assessment_consumer.id
}

output "assessment_consumer_security_group_id" {
  value = aws_security_group.assessment_consumer.id
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
  value = aws_lambda_function.assessment_consumer.function_name
}

output "assessment_consumer_function_arn" {
  value = aws_lambda_function.assessment_consumer.arn
}

output "assessment_consumer_event_source_mapping_uuid" {
  value = aws_lambda_event_source_mapping.assessment_consumer.uuid
}

output "assessment_outbox_relay_function_name" {
  value = aws_lambda_function.assessment_outbox_relay.function_name
}

output "curation_consumer_subnet_id" {
  value = aws_subnet.curation_consumer.id
}

output "curation_consumer_security_group_id" {
  value = aws_security_group.curation_consumer.id
}

output "curation_consumer_log_group_name" {
  value = aws_cloudwatch_log_group.curation_consumer.name
}

output "curation_consumer_parameter_path" {
  value = local.curation_consumer_parameter_path
}

output "curation_dlq_url" {
  value = aws_sqs_queue.curation_dlq.url
}

output "curation_dlq_arn" {
  value = aws_sqs_queue.curation_dlq.arn
}

output "curation_consumer_function_name" {
  value = aws_lambda_function.curation_consumer.function_name
}

output "curation_consumer_function_arn" {
  value = aws_lambda_function.curation_consumer.arn
}

output "curation_consumer_event_source_mapping_uuid" {
  value = aws_lambda_event_source_mapping.curation_consumer.uuid
}

output "curation_outbox_relay_function_name" {
  value = aws_lambda_function.curation_outbox_relay.function_name
}

output "curation_outbox_relay_function_arn" {
  value = aws_lambda_function.curation_outbox_relay.arn
}

output "curation_outbox_relay_log_group_name" {
  value = aws_cloudwatch_log_group.curation_outbox_relay.name
}

output "completion_consumer_subnet_id" {
  value = aws_subnet.completion_consumer.id
}

output "completion_consumer_security_group_id" {
  value = aws_security_group.completion_consumer.id
}

output "completion_consumer_role_arn" {
  value = aws_iam_role.completion_consumer.arn
}

output "completion_consumer_log_group_name" {
  value = aws_cloudwatch_log_group.completion_consumer.name
}

output "completion_dlq_url" {
  value = aws_sqs_queue.completion_dlq.url
}

output "completion_dlq_arn" {
  value = aws_sqs_queue.completion_dlq.arn
}

output "completion_consumer_function_name" {
  value = aws_lambda_function.completion_consumer.function_name
}

output "completion_consumer_function_arn" {
  value = aws_lambda_function.completion_consumer.arn
}

output "completion_consumer_event_source_mapping_uuid" {
  value = aws_lambda_event_source_mapping.completion_consumer.uuid
}

output "completion_outbox_relay_function_name" {
  value = aws_lambda_function.completion_outbox_relay.function_name
}

output "completion_outbox_relay_function_arn" {
  value = aws_lambda_function.completion_outbox_relay.arn
}

output "completion_outbox_relay_log_group_name" {
  value = aws_cloudwatch_log_group.completion_outbox_relay.name
}

output "article_analysis_role_arn" {
  value = aws_iam_role.article_analysis.arn
}

output "article_fetch_role_arn" {
  value = aws_iam_role.article_fetch.arn
}
