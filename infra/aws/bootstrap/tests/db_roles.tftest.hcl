mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:ap-northeast-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/test-policy" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-role" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::test-state" }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}

variables {
  name_prefix  = "slice-test"
  github_owner = "test-owner"
  github_repo  = "test-repo"
  root_domain  = "example.com"
}


run "disabled_until_master_secret_selected" {
  command = plan
  assert {
    condition     = length(aws_iam_role.db_roles) == 0 && length(aws_iam_role_policy.db_roles_execution) == 0
    error_message = "管理secret ARN未指定時は管理ロールを作成しない。"
  }
}
run "dedicated_approved_role_administration" {
  command = plan
  variables {
    db_role_master_secret_arn = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:rds!db-test-123456"
  }
  assert {
    condition     = alltrue([for role in aws_iam_role.db_roles : role.path == "/slice-test-db-admin/"]) && jsondecode(aws_iam_role.db_roles["controller"].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:test-owner/test-repo:environment:production-db-roles"
    error_message = "管理ロールは専用pathと承認environmentのOIDCだけで起動する。"
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.db_roles_execution[0].policy).Statement[3].Resource == var.db_role_master_secret_arn && jsondecode(aws_iam_role_policy.db_roles_execution[0].policy).Statement[3].Action == "secretsmanager:GetSecretValue" && length(jsondecode(aws_iam_role_policy.db_roles_execution[0].policy).Statement) == 4
    error_message = "execution roleは指定secret・backend image・専用logsに限定する。"
  }
  assert {
    condition     = alltrue([for s in jsondecode(aws_iam_role_policy.db_roles_controller[0].policy).Statement : s.Sid != "RunRoleTask" ? true : s.Resource == "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/slice-test-db-roles:*" && s.Condition.ArnEquals["ecs:cluster"] == "arn:aws:ecs:ap-northeast-1:123456789012:cluster/slice-test" && s.Condition.StringEquals["ecs:enable-execute-command"] == "false"])
    error_message = "controllerのRunTaskは専用familyとclusterかつExec無効に限定する。"
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.db_roles_controller[0].policy).Statement[0].Effect == "Deny" && contains(jsondecode(aws_iam_role_policy.db_roles_controller[0].policy).Statement[0].Action, "secretsmanager:GetSecretValue") && length(aws_iam_role_policy.db_roles_controller[0].policy) <= 10240
    error_message = "controllerはsecretを読めずIAMサイズ上限を守る。"
  }
  assert {
    condition     = length(aws_iam_role_policy_attachment.db_roles_guard) == length(aws_iam_role.ci) && jsondecode(aws_iam_policy.db_roles_guard.policy).Statement[0].Effect == "Deny" && contains(jsondecode(aws_iam_policy.db_roles_guard.policy).Statement[0].Action, "iam:PassRole") && jsondecode(aws_iam_policy.db_roles_guard.policy).Statement[1].Action == "ecs:RunTask" && length(aws_iam_policy.db_roles_guard.policy) <= 6144
    error_message = "通常CI全roleは管理ロール変更・PassRole・専用task起動を明示拒否する。"
  }
}
