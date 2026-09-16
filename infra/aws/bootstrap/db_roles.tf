variable "db_role_master_secret_arn" {
  description = "RDS-managed master secret ARN; no secret value is stored in Terraform."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.db_role_master_secret_arn == null ? true : can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:.+$", var.db_role_master_secret_arn))
    error_message = "RDS管理のSecrets Manager ARNを指定する。"
  }
}

locals {
  db_roles_enabled      = var.db_role_master_secret_arn != null
  db_roles_path         = "/${var.name_prefix}-db-admin/"
  db_roles_family       = "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${var.name_prefix}-db-roles:*"
  db_roles_cluster      = "arn:aws:ecs:${var.region}:${local.account_id}:cluster/${var.name_prefix}"
  db_roles_tags         = ["VectorPurpose", "ReleaseSha", "GitHubRunId", "GitHubRunAttempt"]
  db_roles_runtime_arns = [for name in ["exec", "task"] : "arn:aws:iam::${local.account_id}:role/${var.name_prefix}-db-admin/${var.name_prefix}-db-roles-${name}"]
}

resource "aws_iam_role" "db_roles" {
  for_each = local.db_roles_enabled ? toset(["controller", "exec", "task"]) : toset([])
  name     = "${var.name_prefix}-db-roles-${each.key}"
  path     = local.db_roles_path
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = each.key == "controller" ? [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.oidc_host}:aud" = "sts.amazonaws.com"
        "${local.oidc_host}:sub" = "repo:${local.repo}:environment:production-db-roles"
      } }
      }] : [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:ecs:${var.region}:${local.account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "db_roles_execution" {
  count = local.db_roles_enabled ? 1 : 0
  name  = "db-roles-execution"
  role  = aws_iam_role.db_roles["exec"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = "arn:aws:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}/backend" },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/ecs/${var.name_prefix}-db-roles:*" },
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = var.db_role_master_secret_arn },
    ]
  })
}

resource "aws_iam_role_policy" "db_roles_controller" {
  count = local.db_roles_enabled ? 1 : 0
  name  = "db-roles-controller"
  role  = aws_iam_role.db_roles["controller"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "NeverReadCredentials", Effect = "Deny", Action = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"], Resource = "*" },
      {
        Sid = "RegisterApprovedRoleTask", Effect = "Allow", Action = "ecs:RegisterTaskDefinition", Resource = local.db_roles_family
        Condition = {
          StringEquals                = { "aws:RequestTag/VectorPurpose" = "db-roles", "ecs:privileged" = "false" }
          NumericEquals               = { "ecs:task-cpu" = 256, "ecs:task-memory" = 512 }
          Null                        = { "aws:RequestTag/ReleaseSha" = "false", "aws:RequestTag/GitHubRunId" = "false", "aws:RequestTag/GitHubRunAttempt" = "false", "ecs:compute-compatibility" = "false" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = local.db_roles_tags, "ecs:compute-compatibility" = ["FARGATE"] }
        }
      },
      {
        Sid = "RunRoleTask", Effect = "Allow", Action = "ecs:RunTask", Resource = local.db_roles_family
        Condition = {
          StringEquals                = { "aws:RequestTag/VectorPurpose" = "db-roles", "ecs:enable-execute-command" = "false" }
          ArnEquals                   = { "ecs:cluster" = local.db_roles_cluster }
          Null                        = { "aws:RequestTag/ReleaseSha" = "false", "aws:RequestTag/GitHubRunId" = "false", "aws:RequestTag/GitHubRunAttempt" = "false" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = local.db_roles_tags }
        }
      },
      {
        Sid      = "TagAtCreation", Effect = "Allow", Action = "ecs:TagResource"
        Resource = [local.db_roles_family, "arn:aws:ecs:${var.region}:${local.account_id}:task/${var.name_prefix}/*"]
        Condition = {
          StringEquals                = { "aws:RequestTag/VectorPurpose" = "db-roles", "ecs:CreateAction" = ["RegisterTaskDefinition", "RunTask"] }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = local.db_roles_tags }
        }
      },
      {
        Sid       = "StopRoleTask", Effect = "Allow", Action = "ecs:StopTask", Resource = "arn:aws:ecs:${var.region}:${local.account_id}:task/${var.name_prefix}/*"
        Condition = { StringEquals = { "aws:ResourceTag/VectorPurpose" = "db-roles" }, ArnEquals = { "ecs:cluster" = local.db_roles_cluster } }
      },
      {
        Sid       = "InspectDeploymentMetadata", Effect = "Allow"
        Action    = ["ecs:DescribeTasks", "ecs:ListTasks", "ecs:DescribeTaskDefinition", "ecs:ListTagsForResource", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "rds:DescribeDBInstances"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
      },
      { Sid = "ReadBackendDigest", Effect = "Allow", Action = ["ecr:DescribeImages", "ecr:BatchGetImage"], Resource = "arn:aws:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}/backend" },
      { Sid = "PassDedicatedRoles", Effect = "Allow", Action = "iam:PassRole", Resource = local.db_roles_runtime_arns, Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } },
    ]
  })
}

resource "aws_iam_policy" "db_roles_guard" {
  name = "${var.name_prefix}-ci-db-roles-guard"
  path = "/${var.name_prefix}-ci/"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ProtectDatabaseAdminRoles", Effect = "Deny"
        Action   = concat(local.iam_write_actions, ["iam:PassRole", "iam:TagRole", "iam:UntagRole", "iam:UpdateRoleDescription", "sts:AssumeRole"])
        Resource = ["arn:aws:iam::${local.account_id}:role/${var.name_prefix}-db-admin/*", "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-db-admin/*"]
      },
      { Sid = "DenyDatabaseAdminTask", Effect = "Deny", Action = "ecs:RunTask", Resource = local.db_roles_family },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "db_roles_guard" {
  for_each   = aws_iam_role.ci
  role       = each.value.name
  policy_arn = aws_iam_policy.db_roles_guard.arn
}

output "db_roles_role_arns" {
  value = { for key, role in aws_iam_role.db_roles : key => role.arn }
}
