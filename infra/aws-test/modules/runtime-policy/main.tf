variable "account_id" { type = string }
variable "region" { type = string }
variable "run_prefix" { type = string }
variable "db_resource_id" { type = string }
variable "master_secret_arn" { type = string }
variable "gemini_parameter_path" { type = string }

locals {
  arn_prefix = "arn:aws"
  registry   = "${local.arn_prefix}:ecr:${var.region}:${var.account_id}:repository/vector-test"
  logs       = "${local.arn_prefix}:logs:${var.region}:${var.account_id}:log-group:/vector-test/${var.run_prefix}"
  function   = "${local.arn_prefix}:lambda:${var.region}:${var.account_id}:function:${var.run_prefix}-embedding"
  queue      = "${local.arn_prefix}:sqs:${var.region}:${var.account_id}:${var.run_prefix}-embedding"
  eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
  # Agentの管理通信には、任意パラメーターの読取権限を含めない。
  agent_statements = [
    {
      Sid = "SsmAgent", Effect = "Allow", Resource = "*"
      Action = [
        "ssm:UpdateInstanceInformation", "ssm:ListInstanceAssociations", "ssm:DescribeAssociation",
        "ssm:GetDocument", "ssm:DescribeDocument", "ssm:UpdateInstanceAssociationStatus",
        "ssm:UpdateAssociationStatus", "ssm:PutInventory", "ssm:PutComplianceItems",
        "ssm:PutConfigurePackageResult",
      ]
    },
    {
      Sid = "SsmChannels", Effect = "Allow", Resource = "*"
      Action = [
        "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
      ]
    },
    { Sid = "EcrLogin", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
  ]
  ecr_read  = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
  log_write = ["logs:CreateLogStream", "logs:PutLogEvents"]
  no_escalation = {
    Sid    = "NoRoleEscalation", Effect = "Deny", Resource = "*"
    Action = ["iam:*", "sts:AssumeRole", "sts:AssumeRoleWithWebIdentity", "sts:AssumeRoleWithSAML"]
  }
  policies = {
    lambda = jsonencode({
      Version = "2012-10-17"
      Statement = [
        { Sid = "Consume", Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = local.queue },
        { Sid = "AppDatabase", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:${var.region}:${var.account_id}:dbuser:${var.db_resource_id}/vector_app" },
        { Sid = "GeminiKey", Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${var.gemini_parameter_path}" },
        { Sid = "Logs", Effect = "Allow", Action = local.log_write, Resource = "${local.logs}/lambda:*" },
        { Sid = "LambdaNetwork", Effect = "Allow", Action = local.eni_actions, Resource = "*" },
        {
          Sid       = "NoNetworkManagementFromCode", Effect = "Deny", Action = local.eni_actions, Resource = "*"
          Condition = { ArnLike = { "lambda:SourceFunctionArn" = local.function } }
        },
        local.no_escalation,
      ]
    })
    runner = jsonencode({
      Version = "2012-10-17"
      Statement = concat(local.agent_statements, [
        { Sid = "Images", Effect = "Allow", Action = local.ecr_read, Resource = ["${local.registry}/backend", "${local.registry}/proxy"] },
        { Sid = "DatabaseBootstrap", Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = var.master_secret_arn },
        {
          Sid      = "DatabaseRoles", Effect = "Allow", Action = "rds-db:connect"
          Resource = [for role in ["vector", "vector_app"] : "arn:aws:rds-db:${var.region}:${var.account_id}:dbuser:${var.db_resource_id}/${role}"]
        },
        { Sid = "SubmitEvent", Effect = "Allow", Action = ["sqs:SendMessage", "sqs:GetQueueAttributes"], Resource = local.queue },
        { Sid = "Logs", Effect = "Allow", Action = concat(local.log_write, ["logs:DescribeLogStreams"]), Resource = "${local.logs}/runner:*" },
        { Sid = "FindLogGroups", Effect = "Allow", Action = "logs:DescribeLogGroups", Resource = "*" },
        local.no_escalation,
      ])
    })
    proxy = jsonencode({
      Version = "2012-10-17"
      Statement = concat(local.agent_statements, [
        { Sid = "Image", Effect = "Allow", Action = local.ecr_read, Resource = "${local.registry}/proxy" },
        { Sid = "Logs", Effect = "Allow", Action = local.log_write, Resource = "${local.logs}/proxy:*" },
        local.no_escalation,
      ])
    })
  }
}

output "policies" { value = local.policies }
