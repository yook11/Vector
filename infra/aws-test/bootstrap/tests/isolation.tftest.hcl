mock_provider "aws" {
  override_during = plan
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::vector-test-tfstate-123456789012" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:ap-northeast-1:123456789012:repository/vector-test/backend" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/vector-test/bootstrap/mock" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/vector-test/bootstrap/vector-test-terraform" }
  }
}
variables {
  expected_account_id            = "123456789012"
  aws_profile                    = "aws-test-admin"
  trusted_admin_role_arn_pattern = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_TestAdministrator_*"
}

run "reject_malformed_account" {
  command = plan
  variables { expected_account_id = "invalid-account" }
  expect_failures = [var.expected_account_id, var.trusted_admin_role_arn_pattern]
}

run "permanent_storage_and_delegation" {
  command = plan
  assert {
    condition = (
      aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled" &&
      aws_s3_bucket_public_access_block.state.block_public_acls &&
      aws_s3_bucket_public_access_block.state.block_public_policy &&
      aws_s3_bucket_public_access_block.state.ignore_public_acls &&
      aws_s3_bucket_public_access_block.state.restrict_public_buckets &&
      one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "AES256"
    )
    error_message = "stateには暗号化・公開禁止・バージョニングが必要です。"
  }
  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.state.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      one(aws_s3_bucket_lifecycle_configuration.state.rule).noncurrent_version_expiration[0].noncurrent_days == 30
    )
    error_message = "stateにはTLS強制と旧バージョン30日保持が必要です。"
  }
  assert {
    condition     = alltrue([for repo in aws_ecr_repository.images : repo.image_tag_mutability == "IMMUTABLE" && !repo.force_delete])
    error_message = "ECRのタグ変更と強制削除を許可しません。"
  }
  assert {
    condition = (
      jsondecode(aws_iam_policy.state.policy).Statement[1].Condition.StringLike["s3:prefix"][0] == "smoke/*" &&
      endswith(jsondecode(aws_iam_policy.state.policy).Statement[3].Resource, "/smoke/*.tflock") &&
      strcontains(jsondecode(aws_iam_role.terraform.assume_role_policy).Statement[0].Condition.ArnLike["aws:PrincipalArn"], "AWSReservedSSO_TestAdministrator_")
    )
    error_message = "構築ロールはテスト管理者だけが利用し、試験stateとロックだけを操作します。"
  }
  assert {
    condition     = alltrue([for policy in [aws_iam_policy.state, aws_iam_policy.runtime_iam, aws_iam_policy.network_compute, aws_iam_policy.security_group_rules, aws_iam_policy.services] : length(policy.policy) <= 6144])
    error_message = "構築用の管理ポリシーはIAMの6144文字上限以内である必要があります。"
  }
}

run "reject_trust_outside_expected_account" {
  command = plan
  variables {
    trusted_admin_role_arn_pattern = "arn:aws:iam::111111111111:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_TestAdministrator_*"
  }
  expect_failures = [var.trusted_admin_role_arn_pattern]
}

run "security_group_creation_conditions" {
  command = plan
  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_policy.security_group_rules.policy).Statement : statement if statement.Sid == "AuthorizeOnTaggedGroups"]).Resource == "arn:aws:ec2:ap-northeast-1:123456789012:security-group/*" &&
      one([for statement in jsondecode(aws_iam_policy.security_group_rules.policy).Statement : statement if statement.Sid == "AuthorizeOnTaggedGroups"]).Condition.StringEquals == {
        "aws:ResourceTag/Project" = "vector-test", "aws:ResourceTag/Lifecycle" = "smoke"
      }
    )
    error_message = "既存SGへの操作は、そのSGの試験用タグで制限します。"
  }
  assert {
    condition = (
      one([for statement in jsondecode(aws_iam_policy.security_group_rules.policy).Statement : statement if statement.Sid == "CreateTaggedRules"]).Resource == "arn:aws:ec2:ap-northeast-1:123456789012:security-group-rule/*" &&
      one([for statement in jsondecode(aws_iam_policy.security_group_rules.policy).Statement : statement if statement.Sid == "CreateTaggedRules"]).Condition.StringEquals == {
        "aws:RequestTag/Project" = "vector-test", "aws:RequestTag/Lifecycle" = "smoke"
      } &&
      one([for statement in jsondecode(aws_iam_policy.security_group_rules.policy).Statement : statement if statement.Sid == "CreateTaggedRules"]).Condition.Null["aws:RequestTag/RunId"] == "false"
    )
    error_message = "新規SGルールにはRequestTagを使い、RunIdを必須にします。"
  }
  assert {
    condition = (
      alltrue([for statement in jsondecode(aws_iam_policy.security_group_rules.policy).Statement : toset(statement.Action) == toset(["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress"])]) &&
      contains(one([for statement in jsondecode(aws_iam_policy.network_compute.policy).Statement : statement if statement.Sid == "TagOnCreate"]).Condition.StringEquals["ec2:CreateAction"], "AuthorizeSecurityGroupIngress") &&
      contains(one([for statement in jsondecode(aws_iam_policy.network_compute.policy).Statement : statement if statement.Sid == "TagOnCreate"]).Condition.StringEquals["ec2:CreateAction"], "AuthorizeSecurityGroupEgress")
    )
    error_message = "ルール作成と同時のタグ付けに必要な権限を維持します。"
  }
  assert {
    condition = (
      aws_iam_role.terraform.max_session_duration == 3600 &&
      aws_iam_role.terraform.path == "/vector-test/bootstrap/" &&
      alltrue([for policy in aws_iam_policy.runtime_boundary : policy.path == "/vector-test/bootstrap/"]) &&
      alltrue(flatten([for statement in jsondecode(aws_iam_policy.runtime_iam.policy).Statement :
        [for resource in try(tolist(statement.Resource), [statement.Resource]) :
          strcontains(resource, "/vector-test/runtime/") || strcontains(resource, "/vector-test/bootstrap/")
        ]
      ]))
    )
    error_message = "引受は1時間とし、実行ロールと常設権限境界のIAMパスを限定します。"
  }
}

run "smoke_log_management_scope" {
  command = plan
  assert {
    condition = toset(one([for statement in jsondecode(aws_iam_policy.services.policy).Statement : statement if try(statement.Sid, "") == "ManageSmokeLogGroups"]).Resource) == toset([
      "arn:aws:logs:ap-northeast-1:123456789012:log-group:/vector-test/*",
      "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/rds/instance/vector-test-*/postgresql",
      "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/rds/instance/vector-test-*/postgresql:*",
    ])
    error_message = "ログ管理権限はテストアカウントの実行ログと試験RDSのPostgreSQLログだけに限定します。"
  }
}
