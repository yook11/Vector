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

output "migration_task_definition_arn" {
  value = aws_ecs_task_definition.migration.arn
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
