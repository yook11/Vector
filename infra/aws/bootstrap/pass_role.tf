resource "aws_iam_policy" "apply_pass_role" {
  name        = "${var.name_prefix}-ci-apply-pass-role"
  path        = "/${var.name_prefix}-ci/"
  description = "Preserve service and role restrictions independently of deployment policy size."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      # 広いIAM Allowがあるため、PassRoleは明示的なDenyで制限する。
      {
        Sid      = "DenyPassRoleToUnintendedServices"
        Effect   = "Deny"
        Action   = "iam:PassRole"
        Resource = "*"
        Condition = {
          StringNotEquals = {
            "iam:PassedToService" = [
              "ecs-tasks.amazonaws.com",
              "chatbot.amazonaws.com",
              "bedrock-agentcore.amazonaws.com",
              "lambda.amazonaws.com",
              "scheduler.amazonaws.com",
            ]
          }
        }
      },
      # AgentCoreへ渡せるロール名を固定し、別名のロールでの迂回を防ぐ。
      {
        Sid         = "DenyPassRoleToAgentCoreExceptGateway"
        Effect      = "Deny"
        Action      = "iam:PassRole"
        NotResource = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-agentcore-gateway"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "bedrock-agentcore.amazonaws.com"
          }
        }
      },
    ], local.outbox_pass_role_guards)
  })
}

resource "aws_iam_role_policy_attachment" "apply_pass_role" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_pass_role.arn
}
