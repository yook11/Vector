mock_provider "aws" {}

variables {
  run_id              = "contract"
  enable_private_path = false
  enable_bastion      = true
  phase               = "seed"
}

run "reject_invalid_run_id" {
  command = plan
  variables { run_id = "../production" }
  expect_failures = [var.run_id]
}

run "seed_isolated_queues_and_roles" {
  command = plan
  assert {
    condition = (
      local.account_id == "733360597472" &&
      length(aws_sqs_queue.source) == 4 && length(aws_sqs_queue.dlq) == 4 &&
      alltrue([for queue in aws_sqs_queue.source : queue.sqs_managed_sse_enabled]) &&
      alltrue([for queue in aws_sqs_queue.dlq : queue.sqs_managed_sse_enabled]) &&
      alltrue([for name, queue in aws_sqs_queue.dlq : jsondecode(queue.redrive_allow_policy) == {
        redrivePermission = "byQueue", sourceQueueArns = [local.source_arns[name]]
      }])
    )
    error_message = "SSE-SQSを使い、DLQは対応する試験キューだけを受け入れます。"
  }
  assert {
    condition = alltrue([for name, role in aws_iam_role.operations :
      role.path == "/vector-test/dlq-redrive/" && role.max_session_duration == 3600 &&
      jsondecode(role.assume_role_policy).Statement[0] == {
        Effect    = "Allow", Action = "sts:AssumeRole",
        Principal = { AWS = "arn:aws:iam::733360597472:root" },
        Condition = { ArnLike = {
          "aws:PrincipalArn" = "arn:aws:iam::733360597472:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_WorkloadAdministrator_*"
        } }
      }
    ])
    error_message = "試験ロールの引受は同じテストアカウントの管理者だけに限定します。"
  }
  assert {
    condition = alltrue([for name, policy in aws_iam_role_policy.operations :
      jsondecode(policy.policy).Statement[0].Resource == [local.dlq_arns[name]] &&
      toset(jsondecode(policy.policy).Statement[0].Action) == toset([
        "sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask", "sqs:ListMessageMoveTasks",
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
      ]) &&
      jsondecode(policy.policy).Statement[1].Resource == [local.source_arns[name]] &&
      jsondecode(policy.policy).Statement[1].Action == "sqs:SendMessage" &&
      jsondecode(policy.policy).Statement[2].Resource == [local.source_arns[name], local.dlq_arns[name]] &&
      length(jsondecode(policy.policy).Statement) == 3
    ])
    error_message = "各運用ロールは自分のsource/DLQだけを操作します。"
  }
  assert {
    condition     = alltrue([for policy in aws_sqs_queue_policy.source : length(jsondecode(policy.policy).Statement) == 1])
    error_message = "seed段階はTLS強制のみを設定します。"
  }
}

run "verify_policy_matrix" {
  command = plan
  variables { phase = "verify" }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.operations["current"].policy).Statement[1].Condition.StringEquals["aws:CalledViaLast"] == "sqs.amazonaws.com" &&
      alltrue([for name in ["queue-called-via", "queue-via-service", "control"] :
        !can(jsondecode(aws_iam_role_policy.operations[name].policy).Statement[1].Condition)
      ])
    )
    error_message = "IAM側のCalledViaLast条件は現行ケースだけに設定します。"
  }
  assert {
    condition = alltrue([for name in ["current", "queue-called-via", "queue-via-service"] :
      length(jsondecode(aws_sqs_queue_policy.source[name].policy).Statement) == 2 &&
      jsondecode(aws_sqs_queue_policy.source[name].policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_sqs_queue_policy.source[name].policy).Statement[1].Principal == "*" &&
      jsondecode(aws_sqs_queue_policy.source[name].policy).Statement[1].Action == "sqs:SendMessage" &&
      jsondecode(aws_sqs_queue_policy.source[name].policy).Statement[1].Condition.StringNotEquals["aws:sourceVpce"] == "vpce-00000000000000000"
    ])
    error_message = "試験対象のDenyはVPCE外からの直接送信に適用し、ロールの例外を追加しません。"
  }
  assert {
    condition = (
      alltrue([for name in ["current", "queue-called-via"] :
        jsondecode(aws_sqs_queue_policy.source[name].policy).Statement[1].Condition.StringNotEqualsIfExists["aws:CalledViaLast"] == "sqs.amazonaws.com"
      ]) &&
      jsondecode(aws_sqs_queue_policy.source["queue-via-service"].policy).Statement[1].Condition.BoolIfExists["aws:ViaAWSService"] == "false" &&
      length(jsondecode(aws_sqs_queue_policy.source["control"].policy).Statement) == 1
    )
    error_message = "CalledViaLast・ViaAWSService・制限なしの比較条件を固定します。"
  }
}

run "private_path_keeps_credentials_and_permissions_separate" {
  command = apply
  variables {
    enable_private_path = true
    phase               = "verify"
  }
  override_data {
    target = data.aws_ssm_parameter.bastion_ami[0]
    values = { value = "ami-0123456789abcdef0" }
  }
  assert {
    condition = (
      !aws_instance.bastion[0].associate_public_ip_address &&
      aws_instance.bastion[0].metadata_options[0].http_tokens == "required" &&
      aws_instance.bastion[0].root_block_device[0].encrypted &&
      aws_iam_role_policy_attachment.bastion_ssm[0].policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" &&
      jsondecode(aws_iam_role.bastion[0].assume_role_policy).Statement[0].Principal.Service == "ec2.amazonaws.com" &&
      aws_vpc.test[0].enable_dns_support && aws_vpc.test[0].enable_dns_hostnames
    )
    error_message = "踏み台はprivate接続・IMDSv2・暗号化diskとSSM専用ロールで動かします。"
  }
  assert {
    condition = (
      aws_vpc_endpoint.sqs[0].private_dns_enabled &&
      aws_vpc_security_group_egress_rule.bastion_https[0].referenced_security_group_id == aws_security_group.endpoints[0].id &&
      aws_vpc_security_group_ingress_rule.endpoint_https[0].referenced_security_group_id == aws_security_group.bastion[0].id &&
      aws_vpc_security_group_ingress_rule.endpoint_https[0].from_port == 443 &&
      aws_vpc_security_group_ingress_rule.endpoint_https[0].to_port == 443 &&
      alltrue([for name in ["current", "queue-called-via", "queue-via-service"] :
        jsondecode(aws_sqs_queue_policy.source[name].policy).Statement[1].Condition.StringNotEquals["aws:sourceVpce"] == aws_vpc_endpoint.sqs[0].id
      ])
    )
    error_message = "指定VPCEのprivate DNSと踏み台からのHTTPSだけを使います。"
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_vpc_endpoint.sqs[0].policy).Statement :
      contains([for role in aws_iam_role.operations : role.arn], statement.Principal.AWS) &&
      alltrue([for resource in statement.Resource : contains(concat(values(local.source_arns), values(local.dlq_arns)), resource)])
    ])
    error_message = "VPCEは試験運用ロールと試験キューだけを許可します。"
  }
  assert {
    condition = (
      jsondecode(aws_ssm_document.sqs_tunnel[0].content).sessionType == "Port" &&
      keys(jsondecode(aws_ssm_document.sqs_tunnel[0].content).parameters) == ["localPortNumber"] &&
      jsondecode(aws_ssm_document.sqs_tunnel[0].content).properties.host == "sqs.ap-northeast-1.amazonaws.com" &&
      jsondecode(aws_ssm_document.sqs_tunnel[0].content).properties.portNumber == "443" &&
      alltrue([for policy in aws_iam_role_policy.operations_tunnel :
        jsondecode(policy.policy).Statement[0].Resource == "arn:aws:ec2:ap-northeast-1:733360597472:instance/*" &&
        jsondecode(policy.policy).Statement[0].Condition.StringEquals["ssm:resourceTag/vector:session-purpose"] == "sqs-redrive" &&
        jsondecode(policy.policy).Statement[0].Condition.BoolIfExists["ssm:SessionDocumentAccessCheck"] == "true" &&
        jsondecode(policy.policy).Statement[1].Resource == aws_ssm_document.sqs_tunnel[0].arn &&
        jsondecode(policy.policy).Statement[3].Condition.StringEquals["ssm:resourceTag/aws:ssmmessages:session-id"] == "$${aws:userid}"
      ])
    )
    error_message = "SSMは固定宛先documentと本人セッションだけを許可します。"
  }
}

run "temporary_instance_removal_preserves_tag_authorization" {
  command = plan
  variables {
    enable_private_path = true
    enable_bastion      = false
    phase               = "verify"
  }
  assert {
    condition = (
      length(aws_instance.bastion) == 0 &&
      output.fixture.private_connection.instance_id == null &&
      length(aws_ssm_document.sqs_tunnel) == 1 &&
      alltrue([for policy in aws_iam_role_policy.operations_tunnel :
        jsondecode(policy.policy).Statement[0].Resource == "arn:aws:ec2:ap-northeast-1:733360597472:instance/*" &&
        jsondecode(policy.policy).Statement[0].Condition.StringEquals["ssm:resourceTag/vector:session-purpose"] == "sqs-redrive" &&
        !can(jsondecode(policy.policy).Statement[3].Condition.StringEquals["ssm:resourceTag/aws:ssmmessages:target-id"])
      ])
    )
    error_message = "EC2撤去後もSSM認可はタグと本人に限定され、過去のinstance IDに依存しない。"
  }
}
