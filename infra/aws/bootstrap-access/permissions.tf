locals {
  ci_role_arns = {
    for name in ["terraform-plan", "terraform-apply", "app-push", "db-migrate", "app-rollout"] :
    name => "arn:aws:iam::${local.account_id}:role/vector-ci/vector-ci-${name}"
  }
  # bootstrapの管理対象を増やすときは管理者がこの許可リストを先に更新する。
  ci_policy_names = [
    "vector-task-boundary", "vector-agent-task-boundary", "vector-execution-boundary",
    "vector-migration-task-boundary", "vector-migration-execution-boundary",
    "vector-chatbot-boundary", "vector-agentcore-gateway-boundary",
    "vector-outbox-relay-lambda-boundary", "vector-outbox-relay-scheduler-boundary",
    "vector-embedding-consumer-lambda-boundary",
    "vector-assessment-consumer-lambda-boundary",
    "vector-assessment-outbox-relay-lambda-boundary", "vector-assessment-outbox-relay-scheduler-boundary",
    "vector-ci-apply-outbox", "vector-ci-apply-embedding-consumer", "vector-ci-lambda-config-readback",
    "vector-ci-apply-assessment-consumer",
  ]
  ci_policy_arns = [for name in local.ci_policy_names : "arn:aws:iam::${local.account_id}:policy/vector-ci/${name}"]
  attachments = {
    plan = {
      role = local.ci_role_arns["terraform-plan"]
      policies = [
        "arn:aws:iam::aws:policy/ReadOnlyAccess",
        "arn:aws:iam::${local.account_id}:policy/vector-ci/vector-ci-lambda-config-readback",
      ]
    }
    apply = {
      role = local.ci_role_arns["terraform-apply"]
      policies = [for name in ["apply-outbox", "apply-embedding-consumer", "apply-assessment-consumer", "lambda-config-readback"] :
      "arn:aws:iam::${local.account_id}:policy/vector-ci/vector-ci-${name}"]
    }
  }
  service_linked_roles = {
    "ecs.amazonaws.com"                  = "AWSServiceRoleForECS"
    "elasticloadbalancing.amazonaws.com" = "AWSServiceRoleForElasticLoadBalancing"
    "rds.amazonaws.com"                  = "AWSServiceRoleForRDS"
    "elasticache.amazonaws.com"          = "AWSServiceRoleForElastiCache"
    "management.chatbot.amazonaws.com"   = "AWSServiceRoleForAWSChatbot"
  }
  service_linked_role_arns = [for service, name in local.service_linked_roles :
  "arn:aws:iam::${local.account_id}:role/aws-service-role/${service}/${name}"]
  oidc_arn        = "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
  bucket_arn      = "arn:aws:s3:::vector-tfstate-${local.account_id}"
  hosted_zone_arn = "arn:aws:route53:::hostedzone/${var.hosted_zone_id}"

  bootstrap_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ManageKnownCiRoles"
        Effect   = "Allow"
        Resource = values(local.ci_role_arns)
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription",
          "iam:UpdateAssumeRolePolicy", "iam:GetRole", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags",
          "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
        ]
      },
      {
        Sid      = "ManageKnownCiPolicies"
        Effect   = "Allow"
        Resource = local.ci_policy_arns
        Action = [
          "iam:CreatePolicy", "iam:DeletePolicy", "iam:GetPolicy", "iam:GetPolicyVersion",
          "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion",
          "iam:ListPolicyVersions", "iam:ListEntitiesForPolicy", "iam:ListPolicyTags", "iam:TagPolicy", "iam:UntagPolicy",
        ]
      },
      {
        Sid      = "ManageGitHubOidcProvider"
        Effect   = "Allow"
        Resource = local.oidc_arn
        Action = [
          "iam:CreateOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider", "iam:GetOpenIDConnectProvider",
          "iam:UpdateOpenIDConnectProviderThumbprint", "iam:AddClientIDToOpenIDConnectProvider",
          "iam:RemoveClientIDFromOpenIDConnectProvider", "iam:ListOpenIDConnectProviderTags",
          "iam:TagOpenIDConnectProvider", "iam:UntagOpenIDConnectProvider",
        ]
      },
      {
        Sid       = "CreateKnownServiceLinkedRoles"
        Effect    = "Allow"
        Action    = "iam:CreateServiceLinkedRole"
        Resource  = local.service_linked_role_arns
        Condition = { StringEquals = { "iam:AWSServiceName" = keys(local.service_linked_roles) } }
      },
      {
        Sid      = "MaintainServiceLinkedRoles"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:ListRoleTags", "iam:TagRole", "iam:UntagRole", "iam:UpdateRole"]
        Resource = local.service_linked_role_arns
      },
      {
        Sid      = "ManageStateBucketConfiguration"
        Effect   = "Allow"
        Resource = local.bucket_arn
        Action = [
          "s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketAcl", "s3:GetBucketCORS",
          "s3:GetBucketWebsite", "s3:GetBucketVersioning", "s3:GetBucketLogging",
          "s3:GetBucketNotification", "s3:GetBucketRequestPayment", "s3:GetBucketObjectLockConfiguration",
          "s3:GetAccelerateConfiguration", "s3:GetLifecycleConfiguration", "s3:GetReplicationConfiguration",
          "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls",
          "s3:GetBucketTagging", "s3:GetBucketPolicy", "s3:GetBucketPolicyStatus",
          "s3:PutBucketTagging", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration",
          "s3:PutBucketPublicAccessBlock", "s3:PutBucketPolicy",
        ]
      },
      {
        Sid      = "MaintainExistingHostedZone"
        Effect   = "Allow"
        Action   = ["route53:GetHostedZone", "route53:ListTagsForResource", "route53:ChangeTagsForResource", "route53:UpdateHostedZoneComment"]
        Resource = local.hosted_zone_arn
      },
      {
        Sid      = "ReadLambdaConfigurationKeyMetadata"
        Effect   = "Allow"
        Action   = "kms:DescribeKey"
        Resource = "arn:aws:kms:ap-northeast-1:${local.account_id}:key/*"
        Condition = {
          "ForAnyValue:StringEquals" = { "kms:ResourceAliases" = ["alias/aws/lambda"] }
        }
      },
      {
        Sid      = "DenyOwnRoleChanges"
        Effect   = "Deny"
        Action   = "iam:*"
        Resource = local.role_arn
      },
      {
        Sid      = "DenyIdentityAdministrationAndRoleUse"
        Effect   = "Deny"
        Action   = ["sso:*", "sso-directory:*", "identitystore:*", "organizations:*", "iam:PassRole", "sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity"]
        Resource = "*"
      },
      {
        Sid      = "DenySecretValuesAndStateObjects"
        Effect   = "Deny"
        Action   = ["ssm:GetParameter*", "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "kms:Decrypt", "s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"]
        Resource = "*"
      },
      ], [for name, attachment in local.attachments : {
        Sid       = "AttachKnownPolicies${title(name)}"
        Effect    = "Allow"
        Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource  = attachment.role
        Condition = { ArnEquals = { "iam:PolicyARN" = attachment.policies } }
    }])
  }
}

resource "aws_iam_role_policy" "bootstrap_apply" {
  name   = "manage-vector-bootstrap"
  role   = aws_iam_role.bootstrap_apply.id
  policy = jsonencode(local.bootstrap_policy)
}
