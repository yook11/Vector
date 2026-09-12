mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_WorkloadAdministrator_abc123/admin"
    }
  }
}

variables {
  expected_account_id = "123456789012"
  hosted_zone_id      = "Z0123456789EXAMPLE"
}

run "dedicated_sso_entry_and_immutable_executor" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role.bootstrap_apply.assume_role_policy) == jsondecode(jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Sid       = "VectorBootstrapPermissionSet"
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { AWS = "arn:aws:iam::123456789012:root" }
        Condition = { ArnLike = {
          "aws:PrincipalArn" = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_VectorBootstrap_*"
        } }
      }]
    }))
    error_message = "同一アカウントのVectorBootstrapだけを入口にする。"
  }
  assert {
    condition = (
      aws_iam_role.bootstrap_apply.path == "/" &&
      aws_iam_role.bootstrap_apply.name == "vector-bootstrap-apply" &&
      aws_iam_role.bootstrap_apply.max_session_duration == 3600 &&
      jsondecode(output.permission_set_inline_policy).Statement[0].Resource == output.bootstrap_apply_role_arn &&
      length(jsondecode(output.permission_set_inline_policy).Statement) == 1 &&
      jsondecode(output.permission_set_inline_policy).Statement[0].Action == "sts:AssumeRole"
    )
    error_message = "作成済みPermission Setの宛先と一致させ、セッションを1時間に制限する。"
  }
  assert {
    condition = (
      length(aws_iam_role_policy.bootstrap_apply.policy) <= 10240 &&
      alltrue([for s in local.bootstrap_policy.Statement : s.Sid != "DenyOwnRoleChanges" ? true :
        s.Effect == "Deny" && s.Action == "iam:*" && s.Resource == output.bootstrap_apply_role_arn
      ]) &&
      length([for s in local.bootstrap_policy.Statement : s if s.Sid == "DenyOwnRoleChanges"]) == 1 &&
      alltrue([for s in local.bootstrap_policy.Statement : s.Effect != "Allow" ? true :
        !contains(flatten([s.Resource]), output.bootstrap_apply_role_arn) &&
        alltrue([for arn in flatten([s.Resource]) : !strcontains(arn, "vector-test") && !strcontains(arn, "AWSReservedSSO_")])
      ])
    )
    error_message = "容量内のinline policyで自身の直接変更を拒否し、SSOやテストIAMを操作対象に含めない。"
  }
}

run "policy_management_is_delegated_within_ci_path" {
  command = plan
  assert {
    condition = (
      [for s in jsondecode(aws_iam_role_policy.bootstrap_apply.policy).Statement : s.Resource
        if s.Sid == "ManageCiPolicyPath" && s.Effect == "Allow"
      ] == ["arn:aws:iam::123456789012:policy/vector-ci/*"] &&
      alltrue([for resource in [
        { arn = "arn:aws:iam::123456789012:policy/vector-ci/vector-new-consumer-boundary", allowed = true },
        { arn = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-apply-new-consumer", allowed = true },
        { arn = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-plan-new-service", allowed = true },
        { arn = "arn:aws:iam::123456789012:policy/vector-ci/nested/new-policy", allowed = true },
        { arn = "arn:aws:iam::111111111111:policy/vector-ci/vector-new-consumer-boundary", allowed = false },
        { arn = "arn:aws:iam::123456789012:policy/vector-ci-other/new-policy", allowed = false },
        { arn = "arn:aws:iam::123456789012:policy/vector/new-policy", allowed = false },
        { arn = "arn:aws:iam::123456789012:policy/vector-test/bootstrap/new-policy", allowed = false },
        { arn = "arn:aws:iam::aws:policy/AdministratorAccess", allowed = false },
        { arn = "arn:aws:iam::123456789012:role/vector-bootstrap-apply", allowed = false },
        ] : alltrue([for action in ["iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicy"] :
          anytrue([for s in jsondecode(aws_iam_role_policy.bootstrap_apply.policy).Statement :
            s.Effect == "Allow" && contains(flatten([s.Action]), action) &&
            anytrue([for pattern in flatten([s.Resource]) : can(regex("^${replace(pattern, "*", ".*")}$", resource.arn))])
          ]) == resource.allowed
      ])])
    )
    error_message = "新しいpolicyとboundaryの管理は同一アカウントの/vector-ci/配下だけに委譲する。"
  }
  assert {
    condition = alltrue([for s in local.bootstrap_policy.Statement : s.Effect != "Allow" ? true :
      alltrue([for action in flatten([s.Action]) : !strcontains(action, "*")]) &&
      alltrue([for arn in flatten([s.Resource]) : !strcontains(arn, "*") || contains(["ReadLambdaConfigurationKeyMetadata", "ManageCiPolicyPath"], s.Sid)])
    ])
    error_message = "Allowの操作は列挙し、対象ARNのwildcardはCI policy pathとKMS metadataだけに限定する。"
  }
  assert {
    condition = toset(flatten([for s in jsondecode(aws_iam_role_policy.bootstrap_apply.policy).Statement : s.Resource
      if s.Effect == "Allow" && contains(flatten([s.Action]), "iam:CreateRole")
      ])) == toset([for name in ["terraform-plan", "terraform-apply", "app-push", "db-migrate", "app-rollout"] :
    "arn:aws:iam::123456789012:role/vector-ci/vector-ci-${name}"])
    error_message = "管理できるCIロール5つは固定し、新規ロールへ委譲を広げない。"
  }
}

run "policy_attachments_preserve_role_purposes" {
  command = plan
  assert {
    condition = alltrue([for attachment in [
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-apply-new-consumer", roles = ["terraform-apply"] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-plan-new-service", roles = ["terraform-plan"] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-apply-assessment-consumer", roles = ["terraform-apply"] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-apply-embedding-consumer", roles = ["terraform-apply"] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-apply-outbox", roles = ["terraform-apply"] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-lambda-config-readback", roles = ["terraform-plan", "terraform-apply"] },
      { policy = "arn:aws:iam::aws:policy/ReadOnlyAccess", roles = ["terraform-plan"] },
      { policy = "arn:aws:iam::aws:policy/AdministratorAccess", roles = [] },
      { policy = "arn:aws:iam::111111111111:policy/vector-ci/vector-ci-apply-new-consumer", roles = [] },
      { policy = "arn:aws:iam::123456789012:policy/vector/vector-ci-apply-new-consumer", roles = [] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci-other/vector-ci-apply-new-consumer", roles = [] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-ci-lambda-config-readback-other", roles = [] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-new-consumer-boundary", roles = [] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-assessment-consumer-lambda-boundary", roles = [] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-assessment-outbox-relay-lambda-boundary", roles = [] },
      { policy = "arn:aws:iam::123456789012:policy/vector-ci/vector-assessment-outbox-relay-scheduler-boundary", roles = [] },
      ] : alltrue([for role in ["terraform-plan", "terraform-apply", "app-push", "db-migrate", "app-rollout", "new-role"] :
        alltrue([for action in ["iam:AttachRolePolicy", "iam:DetachRolePolicy"] :
          anytrue([for s in jsondecode(aws_iam_role_policy.bootstrap_apply.policy).Statement :
            s.Effect == "Allow" && contains(flatten([s.Action]), action) &&
            contains(flatten([s.Resource]), "arn:aws:iam::123456789012:role/vector-ci/vector-ci-${role}") &&
            try(anytrue([for pattern in s.Condition.ArnLike["iam:PolicyARN"] : can(regex("^${replace(pattern, "*", ".*")}$", attachment.policy))]), false)
          ]) == contains(attachment.roles, role)
        ])
    ])])
    error_message = "既存・新規のpolicy取り付けと取り外しを用途別に限定し、別account・path・boundaryを許可しない。"
  }
  assert {
    condition = toset([for s in jsondecode(aws_iam_role_policy.bootstrap_apply.policy).Statement : s.Resource
      if s.Effect == "Allow" && contains(flatten([s.Action]), "iam:AttachRolePolicy")
      ]) == toset([
      "arn:aws:iam::123456789012:role/vector-ci/vector-ci-terraform-plan",
      "arn:aws:iam::123456789012:role/vector-ci/vector-ci-terraform-apply",
    ])
    error_message = "policyの取り付け先を既存plan/applyに固定する。"
  }
}

run "destructive_operations_and_secret_reads_are_excluded" {
  command = plan
  assert {
    condition = alltrue([for s in local.bootstrap_policy.Statement : s.Effect != "Allow" ? true :
      length(setintersection(toset(flatten([s.Action])), toset([
        "sts:AssumeRole", "iam:PassRole", "iam:CreateUser", "iam:PutRolePermissionsBoundary",
        "s3:GetObject", "s3:PutObject", "s3:DeleteBucket", "route53:DeleteHostedZone",
        "route53:CreateHostedZone", "route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets", "iam:DeleteServiceLinkedRole",
        "kms:Decrypt", "ssm:GetParameter", "secretsmanager:GetSecretValue",
      ]))) == 0
    ])
    error_message = "基盤の破棄・本体state操作・秘密値参照・追加role利用はbootstrap更新に付与しない。"
  }
  assert {
    condition = alltrue([for s in local.bootstrap_policy.Statement : s.Effect != "Allow" ? true :
      !contains(flatten([s.Resource]), "arn:aws:iam::aws:policy/ReadOnlyAccess")
    ])
    error_message = "ARN直接指定のattachmentに管理ポリシー内容の読取権限を追加しない。"
  }
  assert {
    condition = alltrue([for s in local.bootstrap_policy.Statement : s.Sid != "ReadLambdaConfigurationKeyMetadata" ? true :
      s.Action == "kms:DescribeKey" &&
      s.Condition["ForAnyValue:StringEquals"]["kms:ResourceAliases"] == ["alias/aws/lambda"] &&
      s.Resource == "arn:aws:kms:ap-northeast-1:123456789012:key/*"
    ])
    error_message = "bootstrap refreshのKMS参照はLambdaキーのmetadataだけにする。"
  }
}

run "us_east_1_identity_center_has_no_region_path" {
  command = plan
  variables { identity_center_region = "us-east-1" }
  assert {
    condition     = jsondecode(aws_iam_role.bootstrap_apply.assume_role_policy).Statement[0].Condition.ArnLike["aws:PrincipalArn"] == "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_VectorBootstrap_*"
    error_message = "us-east-1のIdentity CenterだけはSSO ARNのregion要素を省略する。"
  }
}

run "executor_cannot_manage_its_own_stack" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
      arn        = "arn:aws:sts::123456789012:assumed-role/vector-bootstrap-apply/bootstrap"
    }
  }
  expect_failures = [aws_iam_role.bootstrap_apply]
}

run "another_account_administrator_is_rejected" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "111111111111"
      arn        = "arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_WorkloadAdministrator_abc123/admin"
    }
  }
  expect_failures = [aws_iam_role.bootstrap_apply]
}

run "wildcard_account_is_rejected" {
  command = plan
  variables { expected_account_id = "*" }
  expect_failures = [var.expected_account_id]
}

run "wildcard_hosted_zone_is_rejected" {
  command = plan
  variables { hosted_zone_id = "*" }
  expect_failures = [var.hosted_zone_id]
}
