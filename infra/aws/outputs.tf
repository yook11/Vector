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

output "egress_subnet_id" {
  value = aws_subnet.public_egress.id
}

output "task_role_arns" {
  description = "段名 -> task role ARN。ECS task definition の taskRoleArn。"
  value       = { for name, role in aws_iam_role.task : name => role.arn }
}

output "execution_role_arns" {
  description = "段名 -> execution role ARN。ECS task definition の executionRoleArn。"
  value       = { for name, role in aws_iam_role.execution : name => role.arn }
}

output "ecr_repository_urls" {
  value = { for name, repo in aws_ecr_repository.this : name => repo.repository_url }
}

output "parameter_store_paths" {
  description = <<-EOT
    段ごとに実値を投入する path。Terraform は作らない。
    aws ssm put-parameter --type SecureString --name /vector/<段>/<key> --value ...
  EOT
  value       = { for name, _ in local.stages : name => "/${var.name_prefix}/${name}/" }
}
