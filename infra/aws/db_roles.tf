resource "aws_security_group" "db_roles" {
  name        = "${var.name_prefix}-db-roles"
  description = "Approved database role administration tasks."
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name_prefix}-db-roles" }
}

resource "aws_security_group" "db_roles_secrets" {
  name        = "${var.name_prefix}-db-roles-secrets"
  description = "Secrets Manager endpoint for database role administration."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_endpoint" "db_roles_secrets" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.migration.id]
  security_group_ids  = [aws_security_group.db_roles_secrets.id]
  private_dns_enabled = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = "secretsmanager:GetSecretValue"
      Resource  = "*"
      Condition = { ArnEquals = { "aws:PrincipalArn" = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-db-admin/${var.name_prefix}-db-roles-exec" } }
    }]
  })
  tags = { Name = "${var.name_prefix}-db-roles-secrets" }
}

locals {
  db_roles_destinations = {
    database    = { sg = aws_security_group.rds.id, port = 5432 }
    images_logs = { sg = aws_security_group.migration_endpoints.id, port = 443 }
    secrets     = { sg = aws_security_group.db_roles_secrets.id, port = 443 }
  }
}

resource "aws_vpc_security_group_egress_rule" "db_roles" {
  for_each                     = local.db_roles_destinations
  security_group_id            = aws_security_group.db_roles.id
  referenced_security_group_id = each.value.sg
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_ingress_rule" "from_db_roles" {
  for_each                     = local.db_roles_destinations
  security_group_id            = each.value.sg
  referenced_security_group_id = aws_security_group.db_roles.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "db_roles_s3" {
  security_group_id = aws_security_group.db_roles.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_cloudwatch_log_group" "db_roles" {
  name              = "/ecs/${var.name_prefix}-db-roles"
  retention_in_days = 30
}

output "db_roles_network" {
  value = {
    subnet_id           = aws_subnet.migration.id
    security_group_id   = aws_security_group.db_roles.id
    secrets_endpoint_id = aws_vpc_endpoint.db_roles_secrets.id
  }
}
