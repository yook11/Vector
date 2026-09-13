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

override_resource {
  override_during = plan
  target          = aws_iam_policy.embedding_consumer_lambda_boundary
  values          = { arn = "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-embedding-consumer-lambda-boundary" }
}

run "lambda_configuration_decrypt_is_restricted" {
  command = plan

  assert {
    condition = (
      data.aws_kms_key.lambda_config.key_id == "alias/aws/lambda" &&
      toset(keys(aws_iam_role_policy_attachment.lambda_config_readback)) == toset(["plan", "apply"]) &&
      alltrue([for name, attachment in aws_iam_role_policy_attachment.lambda_config_readback :
        attachment.role == aws_iam_role.ci[name].name &&
        attachment.policy_arn == aws_iam_policy.lambda_config_readback.arn
      ]) &&
      length(aws_iam_policy.lambda_config_readback.policy) <= 6144
    )
    error_message = "既存Lambdaキーの限定復号は容量内の専用policyとしてplan/applyだけに付与する。"
  }

  assert {
    condition = jsonencode(jsondecode(aws_iam_policy.lambda_config_readback.policy)) == jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid         = "DenyDecryptOtherKeys"
          Effect      = "Deny"
          Action      = "kms:Decrypt"
          NotResource = "arn:aws:kms:ap-northeast-1:123456789012:key/11111111-1111-1111-1111-111111111111"
        },
        {
          Sid      = "DenyDecryptOutsideLambda"
          Effect   = "Deny"
          Action   = "kms:Decrypt"
          Resource = "arn:aws:kms:ap-northeast-1:123456789012:key/11111111-1111-1111-1111-111111111111"
          Condition = {
            StringNotEquals = { "kms:ViaService" = "lambda.ap-northeast-1.amazonaws.com" }
          }
        },
        {
          Sid      = "DenyDecryptOtherFunctions"
          Effect   = "Deny"
          Action   = "kms:Decrypt"
          Resource = "arn:aws:kms:ap-northeast-1:123456789012:key/11111111-1111-1111-1111-111111111111"
          Condition = {
            ArnNotEquals = {
              "kms:EncryptionContext:aws:lambda:FunctionArn" = [
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-outbox-relay",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-embedding-consumer",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-outbox-relay",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-consumer",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-consumer",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-outbox-relay",
              ]
            }
          }
        },
        {
          Sid      = "ReadPipelineLambdaConfiguration"
          Effect   = "Allow"
          Action   = "kms:Decrypt"
          Resource = "arn:aws:kms:ap-northeast-1:123456789012:key/11111111-1111-1111-1111-111111111111"
          Condition = {
            StringEquals = { "kms:ViaService" = "lambda.ap-northeast-1.amazonaws.com" }
            ArnEquals = {
              "kms:EncryptionContext:aws:lambda:FunctionArn" = [
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-outbox-relay",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-embedding-consumer",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-outbox-relay",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-assessment-consumer",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-consumer",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-outbox-relay",
              ]
            }
          }
        },
      ]
    })
    error_message = "対象キー・Lambda経由・2関数が揃う場合だけ許可し、条件欠落や対象外は独立した明示Denyで拒否する。"
  }
}

run "secret_values_and_other_ci_decrypt_remain_denied" {
  command = plan

  assert {
    condition = alltrue([for policy in [
      aws_iam_role_policy.plan_deny_secret_read.policy,
      aws_iam_role_policy.apply.policy,
      aws_iam_role_policy.push.policy,
      aws_iam_role_policy.rollout.policy,
      aws_iam_role_policy.migrate.policy,
      ] :
      alltrue([for sid, expected in {
        NoOwnParameterValues = {
          Sid      = "NoOwnParameterValues"
          Effect   = "Deny"
          Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory", "ssm:GetParametersByPath"]
          Resource = "arn:aws:ssm:*:123456789012:parameter/*"
        }
        NoSecretValues = {
          Sid      = "NoSecretValues"
          Effect   = "Deny"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = "*"
        }
        } :
        [for statement in jsondecode(policy).Statement : jsonencode(statement) if statement.Sid == sid] == [jsonencode(expected)]
      ])
    ])
    error_message = "全CIロールで自アカウントSSMとSecrets Managerの秘密値取得を明示Denyする。"
  }
  assert {
    condition = alltrue([for policy in [
      aws_iam_role_policy.push.policy, aws_iam_role_policy.rollout.policy, aws_iam_role_policy.migrate.policy,
      ] :
      [for statement in jsondecode(policy).Statement : jsonencode(statement) if statement.Sid == "NoDecrypt"] == [
        jsonencode({ Sid = "NoDecrypt", Effect = "Deny", Action = ["kms:Decrypt"], Resource = "*" })
      ]
      ]) && alltrue([for policy in [
        aws_iam_role_policy.plan_deny_secret_read.policy, aws_iam_role_policy.apply.policy,
      ] :
      alltrue([for statement in jsondecode(policy).Statement :
        !(statement.Effect == "Deny" && try(contains(statement.Action, "kms:Decrypt"), statement.Action == "kms:Decrypt"))
      ])
    ])
    error_message = "push/rollout/migrateは全面復号Denyを維持し、plan/applyは専用policyの限定Denyへ移す。"
  }
}

run "consumer_boundary_has_only_required_permissions" {
  command = plan

  assert {
    condition = (
      length(jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement) == 7 &&
      toset([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement : s.Sid]) == toset(["ConsumeEmbeddingEvents", "ReadGeminiKey", "RdsIamAuthAsApp", "WriteConsumerLogs", "ManageLambdaNetworkInterfaces", "DenyEniOperationsFromFunctionCode", "NoPrivilegeEscalation"]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Effect == (contains(["DenyEniOperationsFromFunctionCode", "NoPrivilegeEscalation"], s.Sid) ? "Deny" : "Allow")
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        !contains(["ManageLambdaNetworkInterfaces", "DenyEniOperationsFromFunctionCode"], s.Sid) ? true :
        s.Resource == "*" && toset(s.Action) == toset([
          "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
          "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
        ])
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "ConsumeEmbeddingEvents" ? true :
        s.Effect == "Allow" && toset(s.Action) == toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]) &&
        s.Resource == "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "ReadGeminiKey" ? true :
        s.Action == "ssm:GetParameter" && s.Resource == "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/embedding-consumer/gemini-api-key"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "RdsIamAuthAsApp" ? true :
        s.Action == "rds-db:connect" && s.Resource == "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:*/vector_app"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "WriteConsumerLogs" ? true :
        toset(s.Action) == toset(["logs:CreateLogStream", "logs:PutLogEvents"]) &&
        s.Resource == "arn:aws:logs:ap-northeast-1:123456789012:log-group:/aws/lambda/slice-test-embedding-consumer:*"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.embedding_consumer_lambda_boundary.policy).Statement :
        s.Sid != "DenyEniOperationsFromFunctionCode" ? true :
        s.Effect == "Deny" && toset(s.Action) == toset(local.embedding_consumer_eni_actions) &&
        s.Condition.ArnEquals["lambda:SourceFunctionArn"] == local.embedding_consumer_lambda_arn
      ])
    )
    error_message = "Consumerのboundaryは対象の受信・DB・SSM・ログ・ENIに限定する。"
  }
  assert {
    condition = (
      local.role_boundary_groups.EmbeddingConsumerLambda.role_names == ["slice-test-embedding-consumer-lambda"] &&
      local.role_boundary_groups.EmbeddingConsumerLambda.boundary == aws_iam_policy.embedding_consumer_lambda_boundary.arn &&
      contains(local.managed_role_arns, local.embedding_consumer_role_arn) &&
      !contains(local.app_role_arns, local.embedding_consumer_role_arn) &&
      alltrue([for s in local.boundary_pairing_statements : s.Sid != "DenyWideBoundaryOnEmbeddingConsumerLambdaRoles" ? true :
        s.Resource == [local.embedding_consumer_role_arn] &&
        s.Condition.StringNotEquals["iam:PermissionsBoundary"] == aws_iam_policy.embedding_consumer_lambda_boundary.arn
      ])
    )
    error_message = "Consumerロールの作成を専用boundaryに拘束し、ECS反映対象へ混ぜない。"
  }
}

run "ci_can_manage_dlq_without_granting_relay_send" {
  command = plan

  assert {
    condition = (
      length(aws_iam_role_policy.apply.policy) <= 10240 &&
      length(aws_iam_policy.apply_outbox.policy) <= 6144 &&
      length(aws_iam_policy.embedding_consumer_lambda_boundary.policy) <= 6144
    )
    error_message = "ロール追加後もIAM policyのサイズ上限内に収める。"
  }

  assert {
    condition = (
      toset(local.managed_pipeline_queue_arns) == setunion(toset(local.outbox_queue_arns), toset([local.embedding_dlq_arn, local.assessment_dlq_arn, local.curation_dlq_arn])) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_outbox.policy).Statement : s.Sid != "ManagePipelineQueues" ? true :
        toset(s.Resource) == toset(local.managed_pipeline_queue_arns) &&
        !contains(s.Action, "sqs:SendMessage") && !contains(s.Action, "sqs:ReceiveMessage") && !contains(s.Action, "sqs:PurgeQueue")
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.outbox_relay_lambda_boundary.policy).Statement : s.Sid != "SendPipelineEvents" ? true :
        toset(s.Resource) == toset(local.outbox_queue_arns) && !contains(s.Resource, local.embedding_dlq_arn)
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_outbox.policy).Statement : s.Sid != "ManageOutboxLambda" ? true :
        toset(s.Resource) == toset([local.outbox_lambda_arn, local.assessment_outbox_relay_lambda_arn, local.curation_outbox_relay_lambda_arn])
      ])
    )
    error_message = "CIだけにDLQ管理と専用relayの管理を追加し、既存relayの送信先を維持する。"
  }
}

run "passrole_allows_only_pipeline_lambda_roles" {
  command = plan

  assert {
    condition = (
      length(local.outbox_pass_role_guards) == 4 &&
      toset([for s in local.outbox_pass_role_guards : s.Sid]) == toset([
        "DenyPassRoleToLambdaExceptPipelineRoles", "DenyPipelineLambdaRolesToOtherServices",
        "DenyPassRoleToSchedulerExceptPipelineRoles", "DenyPipelineSchedulerRolesToOtherServices",
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPassRoleToLambdaExceptPipelineRoles" ? true :
        s.Effect == "Deny" && s.Action == "iam:PassRole" &&
        toset(s.NotResource) == toset([
          "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda",
          local.embedding_consumer_role_arn,
          local.assessment_consumer_role_arn,
          local.assessment_outbox_relay_role_arn,
          local.curation_consumer_role_arn,
          local.curation_outbox_relay_role_arn,
        ]) && s.Condition.StringEquals["iam:PassedToService"] == "lambda.amazonaws.com"
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPipelineLambdaRolesToOtherServices" ? true :
        s.Effect == "Deny" && s.Action == "iam:PassRole" &&
        toset(s.Resource) == toset([
          "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda",
          local.embedding_consumer_role_arn,
          local.assessment_consumer_role_arn,
          local.assessment_outbox_relay_role_arn,
          local.curation_consumer_role_arn,
          local.curation_outbox_relay_role_arn,
        ]) && s.Condition.StringNotEquals["iam:PassedToService"] == "lambda.amazonaws.com"
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPassRoleToSchedulerExceptPipelineRoles" ? true :
        s.NotResource == ["arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler", "arn:aws:iam::123456789012:role/slice-test/slice-test-assessment-outbox-relay-scheduler", "arn:aws:iam::123456789012:role/slice-test/slice-test-curation-outbox-relay-scheduler"] &&
        s.Condition.StringEquals["iam:PassedToService"] == "scheduler.amazonaws.com"
      ]) &&
      alltrue([for s in local.outbox_pass_role_guards : s.Sid != "DenyPipelineSchedulerRolesToOtherServices" ? true :
        s.Resource == ["arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-scheduler", "arn:aws:iam::123456789012:role/slice-test/slice-test-assessment-outbox-relay-scheduler", "arn:aws:iam::123456789012:role/slice-test/slice-test-curation-outbox-relay-scheduler"] &&
        s.Effect == "Deny" && s.Condition.StringNotEquals["iam:PassedToService"] == "scheduler.amazonaws.com"
      ]) &&
      alltrue([for guard in local.outbox_pass_role_guards : contains(jsondecode(aws_iam_policy.apply_outbox.policy).Statement, guard)])
    )
    error_message = "LambdaとSchedulerのPassRole制約を双方向に維持する。"
  }
}

run "consumer_management_stays_within_its_boundary" {
  command = plan
  assert {
    condition = (
      length(aws_iam_policy.apply_embedding_consumer.policy) <= 6144 &&
      length(jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement) == 7 &&
      aws_iam_role_policy_attachment.apply_embedding_consumer.role == aws_iam_role.ci["apply"].name &&
      aws_iam_role_policy_attachment.apply_embedding_consumer.policy_arn == aws_iam_policy.apply_embedding_consumer.arn &&
      toset([for s in jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement : s.Sid]) == toset([
        "ManageEmbeddingFunction", "CreateEmbeddingMapping", "ManageEmbeddingMapping",
        "ReadEmbeddingMappingTags", "TagEmbeddingMapping", "UntagEmbeddingMappingMetadata",
        "DenyWideBoundaryOnEmbeddingConsumerLambdaRoles",
      ]) &&
      alltrue([for s in jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement : s.Effect == (s.Sid == "DenyWideBoundaryOnEmbeddingConsumerLambdaRoles" ? "Deny" : "Allow")])
    )
    error_message = "Consumer管理権限は容量内の専用policyとしてapplyだけに付与する。"
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement :
      s.Sid != "ManageEmbeddingFunction" ? true :
      s.Resource == local.embedding_consumer_lambda_arn && toset(s.Action) == toset([
        "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction",
        "lambda:GetFunctionConfiguration", "lambda:GetFunctionCodeSigningConfig",
        "lambda:ListVersionsByFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
        "lambda:GetFunctionConcurrency", "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
        "lambda:GetRuntimeManagementConfig", "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource",
      ])
    ])
    error_message = "関数管理の対象ARNと操作をConsumerだけに限定する。"
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement :
      !contains(["CreateEmbeddingMapping", "ManageEmbeddingMapping"], s.Sid) ? true :
      s.Condition.ArnEquals["lambda:FunctionArn"] == local.embedding_consumer_lambda_arn &&
      (s.Sid == "CreateEmbeddingMapping" ? (
        s.Resource == "*" && s.Action == "lambda:CreateEventSourceMapping" &&
        s.Condition.StringEquals["aws:RequestTag/Consumer"] == "slice-test-embedding-consumer"
        ) : (
        s.Resource == "arn:aws:lambda:ap-northeast-1:123456789012:event-source-mapping:*" &&
        toset(s.Action) == toset(["lambda:GetEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping"]) &&
        s.Condition.StringEquals["aws:ResourceTag/Consumer"] == "slice-test-embedding-consumer"
      ))
    ])
    error_message = "別関数のマッピングを操作できず、作成にも固定タグを必須とする。"
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement :
      !contains(["ReadEmbeddingMappingTags", "TagEmbeddingMapping", "UntagEmbeddingMappingMetadata"], s.Sid) ? true :
      s.Resource == "arn:aws:lambda:ap-northeast-1:123456789012:event-source-mapping:*" &&
      s.Condition.StringEquals["aws:ResourceTag/Consumer"] == "slice-test-embedding-consumer"
      ]) && alltrue([for s in jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement :
      s.Sid != "TagEmbeddingMapping" ? true :
      s.Action == "lambda:TagResource" &&
      s.Condition.StringEqualsIfExists["aws:RequestTag/Consumer"] == "slice-test-embedding-consumer" &&
      toset(s.Condition["ForAllValues:StringEquals"]["aws:TagKeys"]) == toset(["Consumer", "Project", "ManagedBy"])
      ]) && alltrue([for s in jsondecode(aws_iam_policy.apply_embedding_consumer.policy).Statement :
      s.Sid != "UntagEmbeddingMappingMetadata" ? true :
      s.Action == "lambda:UntagResource" &&
      toset(s.Condition["ForAllValues:StringEquals"]["aws:TagKeys"]) == toset(["Project", "ManagedBy"])
    ])
    error_message = "他マッピングへの固定タグ後付け・所属変更・所属タグ削除を許可しない。"
  }
}
