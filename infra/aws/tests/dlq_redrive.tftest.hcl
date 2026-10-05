mock_provider "aws" {
  mock_resource "aws_subnet" {
    defaults = { id = "subnet-00000000000000001" }
  }

  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:test", last_modified = "2026-09-10T00:00:00.000+0000" }
  }

  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:ap-northeast-1:123456789012:certificate/test"
      domain_validation_options = [{
        domain_name           = "app.example.com"
        resource_record_name  = "_test.app.example.com"
        resource_record_type  = "CNAME"
        resource_record_value = "_test.acm-validations.aws."
      }]
    }
  }
  mock_resource "aws_db_instance" {
    defaults = { resource_id = "db-TEST", address = "db.example.com" }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = { id = "vpce-test" }
  }
  mock_resource "aws_route_table" {
    defaults = { id = "rtb-test" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:ap-northeast-1:123456789012:loadbalancer/app/test/1234567890123456" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-role" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ap-northeast-1:123456789012:log-group:test" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      redrive_policy = ""
      arn            = "arn:aws:sqs:ap-northeast-1:123456789012:test"
      url            = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/test"
    }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-northeast-1:123456789012:test-alerts" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:ap-northeast-1:123456789012:repository/test", repository_url = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/test" }
  }
}

override_resource {
  target          = aws_security_group.bastion[0]
  override_during = plan
  values          = { id = "sg-00000000000000001" }
}

override_resource {
  target          = aws_security_group.outbox_sqs_endpoint
  override_during = plan
  values          = { id = "sg-00000000000000002" }
}

variables {
  name_prefix            = "slice-test"
  root_domain            = "example.com"
  frontend_domain        = "app.example.com"
  crossref_contact_email = "test@example.com"
  slack_team_id          = "T0123456789"
  slack_channel_id       = "C0123456789"
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["assessment"]
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment", "url" : "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-assessment" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["embedding"]
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.assessment_dlq
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-assessment-dlq" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.outbox_relay
  values          = { "arn" : "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" }
}

# 初回の基盤準備では、イメージ指定前に処理を開始しない。
run "redrive_preserves_direct_send_boundary" {
  command = plan
  assert {
    condition = alltrue([
      for policy in concat(
        [for queue in aws_sqs_queue_policy.outbox : jsondecode(queue.policy)],
        [jsondecode(aws_sqs_queue_policy.source_dispatch["acquisition"].policy)]
      ) :
      policy.Statement[0].Effect == "Deny" &&
      policy.Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      policy.Statement[1].Effect == "Deny" &&
      policy.Statement[1].Action == "sqs:SendMessage" &&
      policy.Statement[1].Condition.StringNotEquals["aws:sourceVpce"] == "vpce-test" &&
      policy.Statement[1].Condition.StringNotEqualsIfExists["aws:CalledViaLast"] == "sqs.amazonaws.com"
    ])
    error_message = "TLSと直接送信のVPC制限を維持し、SQS代理呼び出しだけを例外にする。"
  }
}

run "operations_endpoint_scope" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement[0].Principal.AWS == "arn:aws:iam::123456789012:role/vector-operations" &&
      jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement[0].Resource == local.operations_dlq_arns &&
      length(local.operations_dlq_arns) == 5 &&
      toset(jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement[0].Action) == toset([
        "sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask", "sqs:ListMessageMoveTasks",
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
      ]) &&
      jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement[1].Principal.AWS == "arn:aws:iam::123456789012:role/vector-operations" &&
      jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement[1].Resource == local.operations_source_arns &&
      length(local.operations_source_arns) == 5 &&
      toset(jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement[1].Action) == toset(["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"])
    )
    error_message = "VPCEの運用許可は対象5組に限定し、通常キューの受信・削除は許可しない。"
  }
}

run "bastion_disabled_has_no_temporary_sqs_path" {
  command = plan
  variables { enable_db_bastion = false }
  assert {
    condition = (
      length(aws_instance.bastion) == 0 &&
      length(aws_vpc_security_group_egress_rule.bastion_to_sqs) == 0 &&
      length(aws_vpc_security_group_ingress_rule.sqs_from_bastion) == 0 &&
      length(aws_vpc_endpoint.ssmmessages) == 0 &&
      output.bastion_instance_id == null
    )
    error_message = "踏み台を無効にすると一時接続経路も撤去される。"
  }
}

run "bastion_enabled_is_ssm_only" {
  command = plan
  variables { enable_db_bastion = true }
  assert {
    condition = (
      length(aws_instance.bastion) == 1 &&
      aws_instance.bastion[0].associate_public_ip_address == false &&
      aws_instance.bastion[0].metadata_options[0].http_tokens == "required" &&
      aws_instance.bastion[0].tags["vector:session-purpose"] == "sqs-redrive" &&
      aws_iam_role_policy_attachment.bastion_ssm[0].policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" &&
      aws_instance.bastion[0].iam_instance_profile == aws_iam_instance_profile.bastion[0].name
    )
    error_message = "EC2はSSM専用のロールと運用タグを持ち、公開入口を持たない。"
  }
  assert {
    condition = (
      aws_vpc_security_group_egress_rule.bastion_to_sqs[0].referenced_security_group_id == aws_security_group.outbox_sqs_endpoint.id &&
      aws_vpc_security_group_egress_rule.bastion_to_sqs[0].security_group_id == aws_security_group.bastion[0].id &&
      aws_vpc_security_group_ingress_rule.sqs_from_bastion[0].security_group_id == aws_security_group.outbox_sqs_endpoint.id &&
      aws_vpc_security_group_ingress_rule.sqs_from_bastion[0].referenced_security_group_id == aws_security_group.bastion[0].id &&
      aws_vpc_security_group_egress_rule.bastion_to_sqs[0].ip_protocol == "tcp" &&
      aws_vpc_security_group_ingress_rule.sqs_from_bastion[0].ip_protocol == "tcp" &&
      aws_vpc_security_group_egress_rule.bastion_to_sqs[0].from_port == 443 &&
      aws_vpc_security_group_egress_rule.bastion_to_sqs[0].to_port == 443 &&
      aws_vpc_security_group_ingress_rule.sqs_from_bastion[0].from_port == 443 &&
      aws_vpc_security_group_ingress_rule.sqs_from_bastion[0].to_port == 443
    )
    error_message = "一時踏み台とSQS endpoint間のTCP443だけを追加する。"
  }
}
