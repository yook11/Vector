resource "aws_iam_policy" "apply_cost_budget" {
  name        = "${var.name_prefix}-ci-apply-cost-budget"
  path        = "/${var.name_prefix}-ci/"
  description = "Manage only the AgentCore daily cost budget and its notification."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Budgets の API は us-east-1 の global endpoint なので、RegionalInfra の region 条件に入れない。
      {
        Sid      = "ManageAgentCoreDailyCostBudget"
        Effect   = "Allow"
        Action   = ["budgets:ViewBudget", "budgets:ModifyBudget", "budgets:ListTagsForResource", "budgets:TagResource", "budgets:UntagResource"]
        Resource = "arn:aws:budgets::${local.account_id}:budget/${var.name_prefix}-agentcore-daily-cost"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "apply_cost_budget" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_cost_budget.arn
}
