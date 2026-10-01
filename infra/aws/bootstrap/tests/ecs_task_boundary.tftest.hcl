mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_kms_key" { defaults = { arn = "arn:aws:kms:ap-northeast-1:123456789012:key/test" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_s3_bucket" { defaults = { arn = "arn:aws:s3:::test" } }
  mock_resource "aws_iam_openid_connect_provider" { defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" } }
}

variables {
  name_prefix  = "slice-test"
  github_owner = "test-owner"
  github_repo  = "test-repo"
  root_domain  = "example.com"
}

run "task_role_ceiling_reaches_only_its_own_db_and_valkey_user" {
  command = plan

  assert {
    condition = [for s in jsondecode(aws_iam_policy.ecs_task_boundary["frontend"].policy).Statement : s if s.Effect == "Allow"] == [
      { Sid = "RdsIamAuth", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_auth" },
      { Sid = "ElastiCacheIamAuth", Effect = "Allow", Action = "elasticache:Connect", Resource = [
        "arn:aws:elasticache:ap-northeast-1:123456789012:replicationgroup:slice-test-rate-limit",
        "arn:aws:elasticache:ap-northeast-1:123456789012:user:slice-test-frontend",
      ] },
    ]
    error_message = "frontend は vector_auth と rate-limit の自分の user だけに届く。"
  }

  assert {
    condition = [for s in jsondecode(aws_iam_policy.ecs_task_boundary["api"].policy).Statement : s if s.Effect == "Allow"] == [
      { Sid = "RdsIamAuth", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_api" },
      { Sid = "ElastiCacheIamAuth", Effect = "Allow", Action = "elasticache:Connect", Resource = [
        "arn:aws:elasticache:ap-northeast-1:123456789012:replicationgroup:slice-test-broker",
        "arn:aws:elasticache:ap-northeast-1:123456789012:user:slice-test-api",
      ] },
    ]
    error_message = "api は vector_api と broker の自分の user だけに届く。"
  }

  assert {
    condition = [for s in jsondecode(aws_iam_policy.ecs_task_boundary["insights"].policy).Statement : s if s.Effect == "Allow"] == [
      { Sid = "RdsIamAuth", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_insights" },
      { Sid = "ElastiCacheIamAuth", Effect = "Allow", Action = "elasticache:Connect", Resource = [
        "arn:aws:elasticache:ap-northeast-1:123456789012:replicationgroup:slice-test-broker",
        "arn:aws:elasticache:ap-northeast-1:123456789012:user:slice-test-insights",
      ] },
    ]
    error_message = "insights は vector_insights と broker の自分の user だけに届く。"
  }

  assert {
    condition = [for s in jsondecode(aws_iam_policy.ecs_task_boundary["agent"].policy).Statement : s if s.Effect == "Allow"] == [
      { Sid = "RdsIamAuth", Effect = "Allow", Action = "rds-db:connect", Resource = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_agent" },
      { Sid = "ElastiCacheIamAuth", Effect = "Allow", Action = "elasticache:Connect", Resource = [
        "arn:aws:elasticache:ap-northeast-1:123456789012:replicationgroup:slice-test-broker",
        "arn:aws:elasticache:ap-northeast-1:123456789012:user:slice-test-agent",
      ] },
      { Sid = "InvokeWebSearchGateway", Effect = "Allow", Action = "bedrock-agentcore:InvokeGateway", Resource = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:gateway/*" },
    ]
    error_message = "agent は vector_agent と broker の自分の user に加え、外部検索の gateway だけに届く。"
  }

  assert {
    condition = [for s in jsondecode(aws_iam_policy.ecs_task_boundary["scheduler"].policy).Statement : s if s.Effect == "Allow"] == [
      { Sid = "ElastiCacheIamAuth", Effect = "Allow", Action = "elasticache:Connect", Resource = [
        "arn:aws:elasticache:ap-northeast-1:123456789012:replicationgroup:slice-test-broker",
        "arn:aws:elasticache:ap-northeast-1:123456789012:user:slice-test-scheduler",
      ] },
    ]
    error_message = "scheduler は DB に届かず、broker の自分の user だけに届く。"
  }

  assert {
    condition     = [for s in jsondecode(aws_iam_policy.ecs_task_boundary["proxy"].policy).Statement : s if s.Effect == "Allow"] == []
    error_message = "proxy は AWS の API を呼ばないので天井に Allow を持たない。"
  }

  assert {
    condition = alltrue([
      for service, policy in aws_iam_policy.ecs_task_boundary :
      [for s in jsondecode(policy.policy).Statement : s.Sid if s.Effect == "Deny"] == ["NoPrivilegeEscalation", "NoEcsExec"]
    ])
    error_message = "どの task role の天井も権限昇格と ECS Exec を拒否する。"
  }
}

run "each_task_role_is_paired_with_its_own_ceiling" {
  command = plan

  override_resource {
    override_during = plan
    target          = aws_iam_policy.ecs_task_boundary["frontend"]
    values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-frontend-task-boundary" }
  }

  override_resource {
    override_during = plan
    target          = aws_iam_policy.ecs_task_boundary["proxy"]
    values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-proxy-task-boundary" }
  }

  assert {
    condition = (
      local.role_boundary_groups["FrontendTask"].role_names == ["slice-test-frontend-task"] &&
      local.boundary_pairing_statements_by_group["FrontendTask"].Resource == ["arn:aws:iam::123456789012:role/slice-test/slice-test-frontend-task"] &&
      local.boundary_pairing_statements_by_group["FrontendTask"].Condition.StringNotEquals["iam:PermissionsBoundary"] == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-frontend-task-boundary" &&
      local.role_boundary_groups["ProxyTask"].role_names == ["slice-test-proxy-task"] &&
      local.boundary_pairing_statements_by_group["ProxyTask"].Condition.StringNotEquals["iam:PermissionsBoundary"] == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-proxy-task-boundary" &&
      !contains(keys(local.role_boundary_groups), "Task")
    )
    error_message = "各 task role は同じサービスの天井でしか作れない。"
  }

  assert {
    condition = (
      contains(local.app_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-frontend-task") &&
      contains(local.app_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-api-task") &&
      contains(local.app_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-insights-task") &&
      contains(local.app_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-agent-task") &&
      contains(local.app_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-scheduler-task") &&
      !contains(local.app_role_arns, "arn:aws:iam::123456789012:role/slice-test/slice-test-proxy-task")
    )
    error_message = "アプリ反映が渡せる task role はアプリサービスの 5 本で、proxy は渡さない。"
  }
}
