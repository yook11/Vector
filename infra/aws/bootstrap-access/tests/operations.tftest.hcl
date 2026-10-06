mock_provider "aws" {
  mock_data "aws_ami" {
    defaults = { id = "ami-00000000000000001", architecture = "arm64", root_device_name = "/dev/xvda" }
  }
  mock_data "aws_network_interface" {
    defaults = { id = "eni-00000000000000001", vpc_id = "vpc-00000000000000001", subnet_id = "subnet-00000000000000001", security_groups = ["sg-00000000000000001"] }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test-role" } }
  mock_resource "aws_iam_instance_profile" { defaults = { arn = "arn:aws:iam::123456789012:instance-profile/test" } }

  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_WorkloadAdministrator_abc123/admin"
    }
  }
}

override_resource {
  target          = aws_ssm_document.sqs_tunnel
  override_during = plan
  values          = { arn = "arn:aws:ssm:ap-northeast-1:123456789012:document/vector-sqs-tunnel" }
}

variables {
  bastion_network     = { vpc_id = "vpc-00000000000000001", subnet_id = "subnet-00000000000000001", security_group_id = "sg-00000000000000001", network_interface_id = "eni-00000000000000001" }
  expected_account_id = "123456789012"
  hosted_zone_id      = "Z0123456789EXAMPLE"
}

run "operations_scope" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_role.operations.assume_role_policy).Statement == [{
        Sid       = "ReadOnlyToOperations"
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { AWS = "arn:aws:iam::123456789012:root" }
        Condition = { ArnLike = {
          "aws:PrincipalArn" = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_ReadOnly_*"
        } }
      }] &&
      aws_iam_role.operations.max_session_duration == 3600 &&
      output.readonly_operations_assume_statement == {
        Sid      = "AssumeVectorOperations"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = output.operations_role_arn
      }
    )
    error_message = "同一アカウントのReadOnly SSOだけを信頼し、1時間の運用セッションと対象ロール限定の引受許可を定義する。"
  }
  assert {
    condition = (
      toset(local.operations_queue_names) == toset([
        "vector-source-acquisition", "vector-article-completion",
        "vector-article-curation", "vector-article-assessment", "vector-article-embedding",
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.operations.policy).Statement :
        alltrue([for action in flatten([s.Action]) : contains([
          "sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask", "sqs:ListMessageMoveTasks",
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl", "sqs:SendMessage",
        ], action)]) &&
        alltrue([for arn in s.Resource : startswith(arn, "arn:aws:sqs:ap-northeast-1:123456789012:vector-") && !strcontains(arn, "*")])
      ])
    )
    error_message = "5工程のキューに限定し、DB・IAM変更・PurgeQueue・ワイルドカードを許可しない。"
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.operations.policy).Statement[0].Resource == local.operations_dlq_arns &&
      jsondecode(aws_iam_role_policy.operations.policy).Statement[1].Resource == local.operations_queue_arns &&
      !can(jsondecode(aws_iam_role_policy.operations.policy).Statement[1].Condition)
    )
    error_message = "受信・削除はDLQだけ、送信は元キューだけに許可し、直接送信の経路制限はキュー側で維持する。"
  }
}

run "operations_fixed_tunnel" {
  command = plan
  assert {
    condition = (
      aws_ssm_document.sqs_tunnel.name == "vector-sqs-tunnel" &&
      aws_ssm_document.sqs_tunnel.document_type == "Session" &&
      jsondecode(aws_ssm_document.sqs_tunnel.content).sessionType == "Port" &&
      keys(jsondecode(aws_ssm_document.sqs_tunnel.content).parameters) == ["localPortNumber"] &&
      jsondecode(aws_ssm_document.sqs_tunnel.content).properties == {
        host            = "sqs.ap-northeast-1.amazonaws.com", portNumber = "443",
        localPortNumber = "{{ localPortNumber }}", type = "LocalPortForwarding"
      }
    )
    error_message = "運用ドキュメントはSQS東京の443だけに接続する。"
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.operations_tunnel.policy).Statement == [
      {
        Sid      = "StartTaggedBastionSession", Effect = "Allow", Action = "ssm:StartSession",
        Resource = "arn:aws:ec2:ap-northeast-1:123456789012:instance/*",
        Condition = {
          StringEquals = { "ssm:resourceTag/vector:session-purpose" = "sqs-redrive" },
          BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
        }
      },
      {
        Sid      = "UseFixedSqsDocument", Effect = "Allow", Action = "ssm:StartSession",
        Resource = aws_ssm_document.sqs_tunnel.arn
      },
      {
        Sid      = "OpenOwnSessionChannel", Effect = "Allow", Action = "ssmmessages:OpenDataChannel",
        Resource = "arn:aws:ssm:ap-northeast-1:123456789012:session/$${aws:userid}-*"
      },
      {
        Sid       = "TerminateOwnSession", Effect = "Allow", Action = "ssm:TerminateSession",
        Resource  = "arn:aws:ssm:ap-northeast-1:123456789012:session/*",
        Condition = { StringEquals = { "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" } }
      }
    ]
    error_message = "タグ付き踏み台・固定document・本人sessionだけを許可し、タグ変更や汎用shellを追加しない。"
  }
}
