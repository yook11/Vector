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

run "bootstrap_inventory_matches_current_resources" {
  command = plan
  assert {
    condition = (
      length(regexall("resource \"aws_iam_policy\"", join("\n", [for path in fileset("../bootstrap", "*.tf") : file("../bootstrap/${path}")]))) == length(local.ci_policy_names) &&
      alltrue([for name in local.ci_policy_names : length(regexall(
        "name\\s*=\\s*\"\\$\\{var.name_prefix\\}${trimprefix(name, "vector")}\"",
        join("\n", [for path in fileset("../bootstrap", "*.tf") : file("../bootstrap/${path}")])
      )) == 1])
    )
    error_message = "bootstrapのmanaged policy追加・改名時には管理者の許可リストも更新する。"
  }
  assert {
    condition = alltrue([for s in local.bootstrap_policy.Statement : s.Effect != "Allow" ? true :
      alltrue([for action in flatten([s.Action]) : !strcontains(action, "*")]) &&
      alltrue([for arn in flatten([s.Resource]) : !strcontains(arn, "*") || s.Sid == "ReadLambdaConfigurationKeyMetadata"])
    ])
    error_message = "Allowの操作と対象を列挙し、KMS alias条件付きmetadata参照以外はARNを固定する。"
  }
  assert {
    condition = alltrue([for s in local.bootstrap_policy.Statement : !startswith(s.Sid, "AttachKnownPolicies") ? true :
      s.Effect == "Allow" && !contains(s.Condition.ArnEquals["iam:PolicyARN"], "arn:aws:iam::aws:policy/AdministratorAccess")
    ])
    error_message = "CIへのmanaged policy付与を既存の対応表に限定する。"
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
