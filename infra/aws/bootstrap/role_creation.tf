resource "aws_iam_policy" "apply_role_creation" {
  name        = "${var.name_prefix}-ci-apply-role-creation"
  path        = "/${var.name_prefix}-ci/"
  description = "Preserve exact role and boundary allowlists independently of inline policy size."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DenyRoleCreationWithoutBoundary"
        Effect = "Deny"
        Action = [
          "iam:CreateRole",
          "iam:CreateUser",
        ]
        Resource = "*"
        Condition = {
          StringNotEquals = {
            "iam:PermissionsBoundary" = [for group in local.role_boundary_groups : group.boundary]
          }
        }
      },
      {
        Sid         = "DenyRoleCreationOutsideKnownRoles"
        Effect      = "Deny"
        Action      = "iam:CreateRole"
        NotResource = local.managed_role_arns
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "apply_role_creation" {
  role       = aws_iam_role.ci["apply"].name
  policy_arn = aws_iam_policy.apply_role_creation.arn
}
