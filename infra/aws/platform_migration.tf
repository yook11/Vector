locals {
  migration_db_url = "postgresql+asyncpg://vector@${local.db_endpoint}/${aws_db_instance.this.db_name}?sslmode=require"
}

resource "aws_ecs_task_definition" "migration_base" {
  family                   = "${var.name_prefix}-migration-base"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  task_role_arn      = aws_iam_role.migration_task.arn
  execution_role_arn = aws_iam_role.migration_execution.arn

  container_definitions = jsonencode([
    {
      name       = "migration"
      image      = "${aws_ecr_repository.this["backend"].repository_url}:${var.image_tag}"
      essential  = true
      privileged = false
      command    = ["python", "-m", "scripts.migration_runner"]
      environment = [
        { name = "ENV", value = "production" },
        { name = "AWS_REGION", value = var.region },
        { name = "DB_IAM_AUTH", value = "true" },
        { name = "MIGRATION_DATABASE_URL", value = local.migration_db_url },
      ]
      secrets = []
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.migration.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = { Name = "${var.name_prefix}-migration-base" }
}

resource "aws_cloudwatch_log_group" "migration" {
  name              = "/ecs/${var.name_prefix}/migration"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.name_prefix}-migration" }
}

resource "aws_iam_role" "migration_task" {
  name                 = "${var.name_prefix}-migration-task"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = local.boundary_arns["migration-task"]
}

resource "aws_iam_role_policy" "migration_task" {
  name = "rds-iam-auth-as-owner"
  role = aws_iam_role.migration_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RdsIamAuthAsOwner"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/vector"
      },
    ]
  })
}

resource "aws_iam_role" "migration_execution" {
  name                 = "${var.name_prefix}-migration-exec"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = local.boundary_arns["migration-execution"]
}

resource "aws_iam_role_policy" "migration_execution" {
  name = "ecs-task-execution"
  role = aws_iam_role.migration_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPullBackendOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.this["backend"].arn
      },
      {
        Sid    = "CloudWatchMigrationLogsOnly"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.migration.arn,
          "${aws_cloudwatch_log_group.migration.arn}:*",
        ]
      },
    ]
  })
}

resource "aws_security_group" "migration_endpoints" {
  name        = "${var.name_prefix}-migration-vpce"
  description = "ECR and CloudWatch Logs endpoints used by migration tasks."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.name_prefix}-migration-vpce" }
}

resource "aws_security_group" "migration" {
  name        = "${var.name_prefix}-migration"
  description = "One-off production database migration task."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.name_prefix}-migration" }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_migration" {
  security_group_id            = aws_security_group.rds.id
  description                  = "production migration"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.migration.id
}

resource "aws_vpc_security_group_egress_rule" "migration_to_rds" {
  security_group_id            = aws_security_group.migration.id
  description                  = "RDS PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_migration" {
  security_group_id            = aws_security_group.migration_endpoints.id
  description                  = "migration"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.migration.id
}

resource "aws_vpc_security_group_egress_rule" "migration_to_endpoints" {
  security_group_id            = aws_security_group.migration.id
  description                  = "ECR / CloudWatch Logs endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.migration_endpoints.id
}

resource "aws_vpc_security_group_egress_rule" "migration_to_s3" {
  security_group_id = aws_security_group.migration.id
  description       = "ECR image layers via S3 Gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
}
