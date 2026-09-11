locals {
  account_id = var.expected_account_id
  role_arn   = "arn:aws:iam::${local.account_id}:role/vector-bootstrap-apply"
  sso_path   = var.identity_center_region == "us-east-1" ? "aws-reserved/sso.amazonaws.com" : "aws-reserved/sso.amazonaws.com/${var.identity_center_region}"
  sso_role   = "arn:aws:iam::${local.account_id}:role/${local.sso_path}/AWSReservedSSO_VectorBootstrap_*"

  permission_set_policy = {
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AssumeVectorBootstrapApply"
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = local.role_arn
    }]
  }
}

resource "aws_iam_role" "bootstrap_apply" {
  name                 = "vector-bootstrap-apply"
  path                 = "/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "VectorBootstrapPermissionSet"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
      Condition = { ArnLike = { "aws:PrincipalArn" = local.sso_role } }
    }]
  })

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        data.aws_caller_identity.current.account_id == local.account_id &&
        can(regex("^arn:aws:sts::${local.account_id}:assumed-role/AWSReservedSSO_WorkloadAdministrator_[0-9a-fA-F]+/.+$", data.aws_caller_identity.current.arn))
      )
      error_message = "bootstrap-accessは対象アカウントのWorkloadAdministratorからだけ管理してください。"
    }
  }
}

output "bootstrap_apply_role_arn" {
  value = local.role_arn
}

output "permission_set_inline_policy" {
  description = "作成済みVectorBootstrapのインラインポリシーとの照合用。"
  value       = jsonencode(local.permission_set_policy)
}
