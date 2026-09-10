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
  target          = aws_sqs_queue.outbox["embedding"]
  values = {
    arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding"
    url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-embedding"
  }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.embedding_dlq
  values = {
    arn = "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding-dlq"
    url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-embedding-dlq"
  }
}

run "queue_redrive_and_sender_isolation" {
  command = plan

  assert {
    condition = (
      aws_sqs_queue.outbox["embedding"].message_retention_seconds == 345600 &&
      aws_sqs_queue.outbox["embedding"].visibility_timeout_seconds == 720 &&
      jsondecode(aws_sqs_queue.outbox["embedding"].redrive_policy).maxReceiveCount == 5 &&
      jsondecode(aws_sqs_queue.outbox["embedding"].redrive_policy).deadLetterTargetArn == aws_sqs_queue.embedding_dlq.arn &&
      aws_sqs_queue.embedding_dlq.message_retention_seconds == 1209600 &&
      !aws_sqs_queue.embedding_dlq.fifo_queue && aws_sqs_queue.embedding_dlq.sqs_managed_sse_enabled
    )
    error_message = "元キューの再配信とDLQの保持・暗号化を維持する。"
  }
  assert {
    condition = (
      jsondecode(aws_sqs_queue_redrive_allow_policy.embedding_dlq.redrive_allow_policy).redrivePermission == "byQueue" &&
      toset(jsondecode(aws_sqs_queue_redrive_allow_policy.embedding_dlq.redrive_allow_policy).sourceQueueArns) == toset([aws_sqs_queue.outbox["embedding"].arn]) &&
      jsondecode(aws_sqs_queue_policy.embedding_dlq.policy).Statement[0].Effect == "Deny" &&
      jsondecode(aws_sqs_queue_policy.embedding_dlq.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false"
    )
    error_message = "DLQはembeddingだけからredriveを受け付け、非TLSを拒否する。"
  }
  assert {
    condition = alltrue([for stage in ["completion", "curation", "assessment"] :
      aws_sqs_queue.outbox[stage].message_retention_seconds == 1209600 &&
      aws_sqs_queue.outbox[stage].visibility_timeout_seconds == 30 &&
      aws_sqs_queue.outbox[stage].redrive_policy == ""
    ])
    error_message = "他工程のキュー設定を変更しない。"
  }
  assert {
    condition = (
      toset(jsondecode(aws_iam_role_policy.outbox_relay.policy).Statement[1].Resource) == toset([for queue in aws_sqs_queue.outbox : queue.arn]) &&
      !contains(jsondecode(aws_iam_role_policy.outbox_relay.policy).Statement[1].Resource, aws_sqs_queue.embedding_dlq.arn) &&
      alltrue([for policy in aws_sqs_queue_policy.outbox :
        jsondecode(policy.policy).Statement[1].Condition.StringNotEquals["aws:sourceVpce"] == aws_vpc_endpoint.outbox_sqs.id
      ])
    )
    error_message = "relayの送信権限と送信元VPCエンドポイント制限を維持する。"
  }
}

run "consumer_network_is_private_and_gemini_only" {
  command = plan

  assert {
    condition = (
      aws_subnet.embedding_consumer.cidr_block == cidrsubnet(var.vpc_cidr, 8, 28) &&
      aws_subnet.embedding_consumer.availability_zone == var.az_primary &&
      !aws_subnet.embedding_consumer.map_public_ip_on_launch &&
      aws_route_table_association.embedding_consumer.route_table_id == aws_route_table.app.id &&
      !contains(keys(local.stages), "embedding_consumer") &&
      !contains(values(local.app_subnet_cidrs), aws_subnet.embedding_consumer.cidr_block)
    )
    error_message = "専用サブネットを既存ECS段から分離してprivate経路に接続する。"
  }
  assert {
    condition = alltrue([for key, endpoint in aws_vpc_endpoint.interface :
      contains(endpoint.security_group_ids, aws_security_group.embedding_consumer_ssm.id) == (key == "ssm")
    ])
    error_message = "Consumerの専用SGはSSMエンドポイントだけに追加する。"
  }
  assert {
    condition = (
      local.proxy_clients.embedding_consumer.cidr == aws_subnet.embedding_consumer.cidr_block &&
      toset(local.proxy_clients.embedding_consumer.domains) == toset(["generativelanguage.googleapis.com"]) &&
      !local.proxy_clients.embedding_consumer.unrestricted &&
      strcontains(local.squid_conf, "acl src_embedding_consumer src ${aws_subnet.embedding_consumer.cidr_block}") &&
      strcontains(local.squid_conf, "http_access allow src_embedding_consumer dst_embedding_consumer") &&
      alltrue([for name in local.egress_stages :
        local.proxy_clients[name].cidr == local.app_subnet_cidrs[name] &&
        toset(local.proxy_clients[name].domains) == toset(flatten([for vendor in local.stages[name].egress_vendors : local.egress_vendor_domains[vendor]])) &&
        local.proxy_clients[name].unrestricted == local.stages[name].egress_unrestricted
      ])
    )
    error_message = "Geminiだけを許可し、既存proxyクライアントは変更しない。"
  }
  assert {
    condition = alltrue([
      for pair in [
        { outbound = aws_vpc_security_group_egress_rule.embedding_consumer_to_rds, inbound = aws_vpc_security_group_ingress_rule.rds_from_embedding_consumer, target = aws_security_group.rds.id, port = 5432 },
        { outbound = aws_vpc_security_group_egress_rule.embedding_consumer_to_proxy, inbound = aws_vpc_security_group_ingress_rule.proxy_from_embedding_consumer, target = aws_security_group.proxy.id, port = var.proxy_port },
        { outbound = aws_vpc_security_group_egress_rule.embedding_consumer_to_ssm, inbound = aws_vpc_security_group_ingress_rule.ssm_from_embedding_consumer, target = aws_security_group.embedding_consumer_ssm.id, port = 443 },
      ] :
      pair.outbound.security_group_id == aws_security_group.embedding_consumer.id &&
      pair.outbound.referenced_security_group_id == pair.target &&
      pair.inbound.security_group_id == pair.target &&
      pair.inbound.referenced_security_group_id == aws_security_group.embedding_consumer.id &&
      pair.outbound.from_port == pair.port && pair.outbound.to_port == pair.port &&
      pair.inbound.from_port == pair.port && pair.inbound.to_port == pair.port &&
      pair.outbound.ip_protocol == "tcp" && pair.inbound.ip_protocol == "tcp"
    ])
    error_message = "RDS・proxy・SSMの各経路の両側を必要なポートだけで許可する。"
  }
}

run "consumer_permissions_are_scoped" {
  command = plan

  assert {
    condition = (
      aws_iam_role.embedding_consumer.name == "slice-test-embedding-consumer-lambda" &&
      aws_iam_role.embedding_consumer.permissions_boundary == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-embedding-consumer-lambda-boundary" &&
      jsondecode(aws_iam_role.embedding_consumer.assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com"
    )
    error_message = "専用boundaryとLambdaの信頼関係を維持する。"
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement) == 6 &&
      toset([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement : s.Sid]) == toset(["ConsumeEmbeddingEvents", "ReadGeminiKey", "RdsIamAuthAsApp", "WriteConsumerLogs", "ManageLambdaNetworkInterfaces", "DenyEniOperationsFromFunctionCode"]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement :
        s.Effect == (contains(["DenyEniOperationsFromFunctionCode", "NoPrivilegeEscalation"], s.Sid) ? "Deny" : "Allow")
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement :
        !contains(["ManageLambdaNetworkInterfaces", "DenyEniOperationsFromFunctionCode"], s.Sid) ? true :
        s.Resource == "*" && toset(s.Action) == toset([
          "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
          "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
        ])
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement :
        s.Sid != "ConsumeEmbeddingEvents" ? true :
        toset(s.Action) == toset(["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]) && s.Resource == aws_sqs_queue.outbox["embedding"].arn
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement :
        s.Sid != "ReadGeminiKey" ? true :
        s.Action == "ssm:GetParameter" && s.Resource == "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/embedding-consumer/gemini-api-key"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement :
        s.Sid != "RdsIamAuthAsApp" ? true :
        s.Action == "rds-db:connect" && s.Resource == "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:${aws_db_instance.this.resource_id}/vector_app"
      ]) &&
      alltrue([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement :
        s.Sid != "WriteConsumerLogs" ? true :
        toset(s.Action) == toset(["logs:CreateLogStream", "logs:PutLogEvents"]) && s.Resource == "${aws_cloudwatch_log_group.embedding_consumer.arn}:*"
      ])
    )
    error_message = "SQS・SSM・DB・ログの権限をConsumerの対象に限定する。"
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.embedding_consumer.policy).Statement :
      s.Sid != "DenyEniOperationsFromFunctionCode" ? true :
      s.Effect == "Deny" && toset(s.Action) == toset(local.embedding_consumer_eni_actions) &&
      s.Condition.ArnEquals["lambda:SourceFunctionArn"] == "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-embedding-consumer"
    ])
    error_message = "関数コードからのENI操作を明示的に拒否する。"
  }
}

run "dlq_notification_without_consumer_activation" {
  command = plan

  assert {
    condition = (
      aws_cloudwatch_log_group.embedding_consumer.retention_in_days == var.log_retention_days &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.namespace == "AWS/SQS" &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.metric_name == "ApproximateNumberOfMessagesVisible" &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.dimensions.QueueName == aws_sqs_queue.embedding_dlq.name &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.statistic == "Maximum" &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.period == 60 &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.evaluation_periods == 1 &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.datapoints_to_alarm == 1 &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.threshold == 1 &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.comparison_operator == "GreaterThanOrEqualToThreshold" &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.treat_missing_data == "notBreaching" &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.alarm_actions == toset([aws_sns_topic.alerts.arn]) &&
      aws_cloudwatch_metric_alarm.embedding_dlq_not_empty.ok_actions == toset([aws_sns_topic.alerts.arn]) &&
      length(aws_lambda_function.outbox_relay) == 0
    )
    error_message = "DLQ滞留通知だけを既存SNSへ接続し、初回構築でrelayを起動しない。"
  }
}

override_resource {
  override_during = plan
  target          = aws_security_group.embedding_consumer
  values          = { id = "sg-00000000000000000" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.embedding_consumer_ssm
  values          = { id = "sg-00000000000000001" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.endpoints
  values          = { id = "sg-00000000000000002" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.migration_endpoints
  values          = { id = "sg-00000000000000003" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.rds
  values          = { id = "sg-00000000000000004" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.proxy
  values          = { id = "sg-00000000000000005" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.outbox_sqs_endpoint
  values          = { id = "sg-00000000000000006" }
}

run "without_digest_no_consumer_or_mapping" {
  command = plan
  assert {
    condition = (
      length(aws_lambda_function.embedding_consumer) == 0 &&
      length(aws_lambda_event_source_mapping.embedding_consumer) == 0 &&
      output.embedding_consumer_function_name == null &&
      output.embedding_consumer_function_arn == null &&
      output.embedding_consumer_image_digest == null &&
      output.embedding_consumer_event_source_mapping_uuid == null
    )
    error_message = "初回digest未指定ではConsumerもトリガーも作成しない。"
  }
}

run "consumer_image_and_disabled_mapping" {
  command = plan
  variables {
    embedding_consumer_image_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    outbox_relay_image_digest       = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
  assert {
    condition = (
      aws_lambda_function.embedding_consumer[0].image_uri == "${aws_ecr_repository.this["backend"].repository_url}@${var.embedding_consumer_image_digest}" &&
      aws_lambda_function.outbox_relay[0].image_uri == "${aws_ecr_repository.this["backend"].repository_url}@${var.outbox_relay_image_digest}" &&
      aws_lambda_function.embedding_consumer[0].function_name == "slice-test-embedding-consumer" &&
      aws_lambda_function.embedding_consumer[0].package_type == "Image" &&
      aws_lambda_function.embedding_consumer[0].architectures == tolist(["arm64"]) &&
      aws_lambda_function.embedding_consumer[0].memory_size == 1024 &&
      aws_lambda_function.embedding_consumer[0].timeout == 120 &&
      aws_lambda_function.embedding_consumer[0].reserved_concurrent_executions == 10 &&
      aws_lambda_function.outbox_relay[0].reserved_concurrent_executions == 1 &&
      aws_scheduler_schedule.outbox_relay[0].state == "DISABLED"
    )
    error_message = "Consumerのイメージと実行上限をrelayから独立して設定する。"
  }
  assert {
    condition = (
      aws_lambda_function.embedding_consumer[0].image_config[0].entry_point == tolist(["/app/.venv/bin/python", "-m", "awslambdaric"]) &&
      aws_lambda_function.embedding_consumer[0].image_config[0].command == tolist(["app.lambda_handlers.embedding.handler"]) &&
      aws_lambda_function.embedding_consumer[0].image_config[0].working_directory == "/app" &&
      aws_lambda_function.embedding_consumer[0].vpc_config[0].subnet_ids == toset([aws_subnet.embedding_consumer.id]) &&
      aws_lambda_function.embedding_consumer[0].vpc_config[0].security_group_ids == toset([aws_security_group.embedding_consumer.id]) &&
      aws_lambda_function.embedding_consumer[0].role == aws_iam_role.embedding_consumer.arn &&
      aws_lambda_function.embedding_consumer[0].logging_config[0].log_group == aws_cloudwatch_log_group.embedding_consumer.name &&
      aws_lambda_function.embedding_consumer[0].logging_config[0].log_format == "Text" &&
      aws_lambda_function.embedding_consumer[0].tracing_config[0].mode == "PassThrough"
    )
    error_message = "既存入口を専用ネットワーク・権限・ログへ接続する。"
  }
  assert {
    condition = aws_lambda_function.embedding_consumer[0].environment[0].variables == tomap({
      ENV                           = "production"
      DATABASE_URL                  = local.backend_db_url["vector_app"]
      DB_IAM_AUTH                   = "true"
      GEMINI_API_KEY_PARAMETER_PATH = local.embedding_consumer_parameter_path
      EGRESS_PROXY_URL              = local.proxy_url
    })
    error_message = "秘密値・AWS予約変数を渡さず、IAM・専用SSM・プロキシ設定だけを渡す。"
  }
  assert {
    condition = (
      aws_lambda_event_source_mapping.embedding_consumer[0].function_name == aws_lambda_function.embedding_consumer[0].arn &&
      aws_lambda_event_source_mapping.embedding_consumer[0].event_source_arn == aws_sqs_queue.outbox["embedding"].arn &&
      !aws_lambda_event_source_mapping.embedding_consumer[0].enabled &&
      aws_lambda_event_source_mapping.embedding_consumer[0].batch_size == 1 &&
      aws_lambda_event_source_mapping.embedding_consumer[0].maximum_batching_window_in_seconds == 0 &&
      aws_lambda_event_source_mapping.embedding_consumer[0].scaling_config[0].maximum_concurrency == 10 &&
      aws_lambda_event_source_mapping.embedding_consumer[0].function_response_types == toset(["ReportBatchItemFailures"]) &&
      aws_lambda_event_source_mapping.embedding_consumer[0].tags.Consumer == "slice-test-embedding-consumer" &&
      output.embedding_consumer_image_digest == var.embedding_consumer_image_digest
    )
    error_message = "1件ずつの部分バッチ応答を設定するが、受信は開始しない。"
  }
  assert {
    condition = (
      length(jsondecode(aws_ecr_repository_policy.outbox_relay.policy).Statement) == 1 &&
      toset(jsondecode(aws_ecr_repository_policy.outbox_relay.policy).Statement[0].Condition.ArnLike["aws:SourceArn"]) == toset([local.outbox_relay_arn, local.embedding_consumer_arn]) &&
      jsondecode(aws_ecr_repository_policy.outbox_relay.policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_ecr_repository_policy.outbox_relay.policy).Statement[0].Principal.Service == "lambda.amazonaws.com" &&
      toset(jsondecode(aws_ecr_repository_policy.outbox_relay.policy).Statement[0].Action) == toset(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"])
    )
    error_message = "backend ECRからの取得は同一アカウントのrelayとConsumerだけに許可する。"
  }
}

run "reject_mutable_image_tag" {
  command = plan
  variables {
    embedding_consumer_image_digest = "latest"
  }
  expect_failures = [var.embedding_consumer_image_digest]
}
