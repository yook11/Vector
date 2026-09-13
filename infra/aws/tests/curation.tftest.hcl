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
  target          = aws_sqs_queue.outbox["curation"]
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation", "url" : "https://sqs.ap-northeast-1.amazonaws.com/123456789012/slice-test-article-curation" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.outbox["embedding"]
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-embedding" }
}

override_resource {
  override_during = plan
  target          = aws_sqs_queue.curation_dlq
  values          = { "arn" : "arn:aws:sqs:ap-northeast-1:123456789012:slice-test-article-curation-dlq" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.curation_outbox_relay
  values          = { "arn" : "arn:aws:iam::123456789012:role/slice-test/slice-test-curation-outbox-relay-lambda" }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.outbox_relay
  values          = { "arn" : "arn:aws:iam::123456789012:role/slice-test/slice-test-outbox-relay-lambda" }
}


# 初回の基盤準備では、イメージ指定前に処理を開始しない。
run "without_digests_no_curation_triggers" {
  command = plan
  assert {
    condition = (
      length(aws_lambda_function.curation_consumer) == 0 &&
      length(aws_lambda_event_source_mapping.curation_consumer) == 0 &&
      length(aws_lambda_function.curation_outbox_relay) == 0 &&
      length(aws_scheduler_schedule.curation_outbox_relay) == 0
    )
    error_message = "digest未指定では両Lambdaと起動トリガーを作成しない。"
  }
}

# Consumerだけを指定してもrelayは起動せず、対象キューから既存と同じ上限で受信する。
run "consumer_receives_curation_queue_with_embedding_limits" {
  command = plan
  variables {
    curation_consumer_image_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  assert {
    condition = (
      aws_lambda_function.curation_consumer[0].image_config[0].command == tolist(["app.lambda_handlers.curation.handler.handler"]) &&
      aws_lambda_function.curation_consumer[0].image_uri == "${aws_ecr_repository.this["backend"].repository_url}@${var.curation_consumer_image_digest}" &&
      aws_lambda_function.curation_consumer[0].vpc_config[0].subnet_ids == toset([aws_subnet.curation_consumer.id]) &&
      aws_lambda_function.curation_consumer[0].vpc_config[0].security_group_ids == toset([aws_security_group.curation_consumer.id]) &&
      aws_lambda_function.curation_consumer[0].architectures == tolist(["arm64"]) &&
      aws_lambda_function.curation_consumer[0].memory_size == 1024 &&
      aws_lambda_function.curation_consumer[0].timeout == 120 &&
      aws_lambda_function.curation_consumer[0].reserved_concurrent_executions == 10 &&
      aws_lambda_function.curation_consumer[0].environment[0].variables == tomap({
        ENV                           = "production"
        DATABASE_URL                  = local.backend_db_url["vector_app"]
        DB_IAM_AUTH                   = "true"
        GEMINI_API_KEY_PARAMETER_PATH = "/slice-test/curation-consumer/gemini-api-key"
        EGRESS_PROXY_URL              = local.proxy_url
      }) &&
      aws_lambda_event_source_mapping.curation_consumer[0].event_source_arn == aws_sqs_queue.outbox["curation"].arn &&
      aws_lambda_event_source_mapping.curation_consumer[0].function_name == aws_lambda_function.curation_consumer[0].arn &&
      aws_lambda_event_source_mapping.curation_consumer[0].enabled &&
      aws_lambda_event_source_mapping.curation_consumer[0].batch_size == 1 &&
      aws_lambda_event_source_mapping.curation_consumer[0].maximum_batching_window_in_seconds == 0 &&
      aws_lambda_event_source_mapping.curation_consumer[0].scaling_config[0].maximum_concurrency == 10 &&
      aws_lambda_event_source_mapping.curation_consumer[0].function_response_types == toset(["ReportBatchItemFailures"]) &&
      length(aws_lambda_function.curation_outbox_relay) == 0
    )
    error_message = "Curationの入口・秘密情報参照・SQS受信上限を正しく接続する。"
  }
}

# 失敗メッセージは専用DLQへ移し、既存SNSで滞留を通知する。
run "curation_redrive_and_dlq_notification" {
  command = plan
  assert {
    condition = (
      aws_sqs_queue.outbox["curation"].message_retention_seconds == 345600 &&
      aws_sqs_queue.outbox["curation"].visibility_timeout_seconds == 720 &&
      jsondecode(aws_sqs_queue.outbox["curation"].redrive_policy) == {
        deadLetterTargetArn = aws_sqs_queue.curation_dlq.arn
        maxReceiveCount     = 5
      } &&
      aws_sqs_queue.curation_dlq.message_retention_seconds == 1209600 &&
      aws_sqs_queue.curation_dlq.sqs_managed_sse_enabled &&
      jsondecode(aws_sqs_queue_redrive_allow_policy.curation_dlq.redrive_allow_policy).sourceQueueArns == [aws_sqs_queue.outbox["curation"].arn] &&
      aws_cloudwatch_metric_alarm.curation_dlq_not_empty.dimensions.QueueName == aws_sqs_queue.curation_dlq.name &&
      aws_cloudwatch_metric_alarm.curation_dlq_not_empty.threshold == 1 &&
      aws_cloudwatch_metric_alarm.curation_dlq_not_empty.alarm_actions == toset([aws_sns_topic.alerts.arn])
    )
    error_message = "Curationの再配信・専用DLQ・滞留通知を接続する。"
  }
}

# Consumerの通信先とデータ権限をCurationの対象に限定する。
run "consumer_network_and_permissions_are_scoped" {
  command = plan
  assert {
    condition = (
      aws_subnet.curation_consumer.cidr_block == cidrsubnet(var.vpc_cidr, 8, 32) &&
      !aws_subnet.curation_consumer.map_public_ip_on_launch &&
      !contains(values(local.app_subnet_cidrs), aws_subnet.curation_consumer.cidr_block) &&
      aws_subnet.curation_consumer.cidr_block != aws_subnet.embedding_consumer.cidr_block &&
      aws_route_table_association.curation_consumer.route_table_id == aws_route_table.app.id &&
      local.proxy_clients.curation_consumer.domains == ["generativelanguage.googleapis.com"] &&
      local.proxy_clients.curation_consumer.cidr == aws_subnet.curation_consumer.cidr_block &&
      !local.proxy_clients.curation_consumer.unrestricted &&
      strcontains(local.squid_conf, "http_access allow src_curation_consumer dst_curation_consumer") &&
      alltrue([for key, endpoint in aws_vpc_endpoint.interface :
        contains(endpoint.security_group_ids, aws_security_group.curation_consumer_ssm.id) == (key == "ssm")
      ])
    )
    error_message = "専用private subnetとGemini・SSMの接続を固定する。"
  }
  assert {
    condition = alltrue([for pair in [
      { outbound = aws_vpc_security_group_egress_rule.curation_consumer_to_rds, inbound = aws_vpc_security_group_ingress_rule.rds_from_curation_consumer, target = aws_security_group.rds.id, port = 5432 },
      { outbound = aws_vpc_security_group_egress_rule.curation_consumer_to_proxy, inbound = aws_vpc_security_group_ingress_rule.proxy_from_curation_consumer, target = aws_security_group.proxy.id, port = var.proxy_port },
      { outbound = aws_vpc_security_group_egress_rule.curation_consumer_to_ssm, inbound = aws_vpc_security_group_ingress_rule.ssm_from_curation_consumer, target = aws_security_group.curation_consumer_ssm.id, port = 443 },
      ] :
      pair.outbound.security_group_id == aws_security_group.curation_consumer.id &&
      pair.outbound.referenced_security_group_id == pair.target &&
      pair.inbound.security_group_id == pair.target &&
      pair.inbound.referenced_security_group_id == aws_security_group.curation_consumer.id &&
      pair.outbound.from_port == pair.port && pair.outbound.to_port == pair.port &&
      pair.inbound.from_port == pair.port && pair.inbound.to_port == pair.port
    ])
    error_message = "DB・proxy・SSMへの通信を両側のSGで限定する。"
  }
  assert {
    condition = (
      aws_iam_role.curation_consumer.permissions_boundary == "arn:aws:iam::123456789012:policy/slice-test-ci/slice-test-curation-consumer-lambda-boundary" &&
      { for s in jsondecode(aws_iam_role_policy.curation_consumer.policy).Statement : s.Sid => s.Resource if contains(["ConsumeCurationEvents", "RdsIamAuthAsApp", "ReadGeminiKey"], s.Sid) } == {
        ConsumeCurationEvents = aws_sqs_queue.outbox["curation"].arn
        RdsIamAuthAsApp       = "arn:aws:rds-db:ap-northeast-1:123456789012:dbuser:db-TEST/vector_app"
        ReadGeminiKey         = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/slice-test/curation-consumer/gemini-api-key"
      }
    )
    error_message = "Consumerは対象キュー・vector_app・専用キーを使用する。"
  }
}

# 両digestの指定で、Curationの配送と受信を有効にする。
run "relay_sends_only_to_curation_queue" {
  command = plan
  variables {
    curation_consumer_image_digest     = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    curation_outbox_relay_image_digest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
  assert {
    condition = (
      aws_lambda_function.curation_outbox_relay[0].image_config[0].command == tolist(["app.lambda_handlers.outbox_relay.curation_handler"]) &&
      aws_lambda_function.curation_outbox_relay[0].image_uri == "${aws_ecr_repository.this["backend"].repository_url}@${var.curation_outbox_relay_image_digest}" &&
      aws_lambda_function.curation_outbox_relay[0].vpc_config[0].subnet_ids == toset([aws_subnet.app["api"].id]) &&
      aws_lambda_function.curation_outbox_relay[0].vpc_config[0].security_group_ids == toset([aws_security_group.outbox_relay.id]) &&
      aws_lambda_function.curation_outbox_relay[0].architectures == tolist(["arm64"]) &&
      aws_lambda_function.curation_outbox_relay[0].memory_size == 512 &&
      aws_lambda_function.curation_outbox_relay[0].timeout == 120 &&
      aws_lambda_function.curation_outbox_relay[0].reserved_concurrent_executions == 1 &&
      aws_lambda_function.curation_outbox_relay[0].environment[0].variables == tomap({
        ENV                            = "production"
        DATABASE_URL                   = local.backend_db_url["vector_app"]
        DB_IAM_AUTH                    = "true"
        SQS_ARTICLE_CURATION_QUEUE_URL = aws_sqs_queue.outbox["curation"].url
      }) &&
      aws_scheduler_schedule.curation_outbox_relay[0].schedule_expression == "rate(1 minute)" &&
      aws_scheduler_schedule.curation_outbox_relay[0].state == "ENABLED" &&
      aws_scheduler_schedule.curation_outbox_relay[0].target[0].arn == aws_lambda_function.curation_outbox_relay[0].arn &&
      jsondecode(aws_iam_role_policy.curation_outbox_relay_scheduler.policy).Statement[0].Resource == local.curation_outbox_relay_arn &&
      length(aws_lambda_function.curation_consumer) == 1 &&
      length(aws_lambda_event_source_mapping.curation_consumer) == 1
    )
    error_message = "専用relayの入口と1分間隔の起動を接続する。"
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.curation_outbox_relay.policy).Statement[1].Resource == aws_sqs_queue.outbox["curation"].arn &&
      [for s in jsondecode(aws_vpc_endpoint.outbox_sqs.policy).Statement : s.Resource if s.Principal.AWS == aws_iam_role.curation_outbox_relay.arn] == [aws_sqs_queue.outbox["curation"].arn]
    )
    error_message = "専用relayは実行ロールとVPC endpointの両方でCurationキューだけへ送信できる。"
  }
}

# mutable tagは両方の入口で受け付けない。
run "reject_curation_image_tags" {
  command = plan
  variables {
    curation_consumer_image_digest     = "latest"
    curation_outbox_relay_image_digest = "latest"
  }
  expect_failures = [var.curation_consumer_image_digest, var.curation_outbox_relay_image_digest]
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


override_resource {
  override_during = plan
  target          = aws_security_group.curation_consumer
  values          = { id = "sg-00000000000000009" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.curation_consumer_ssm
  values          = { id = "sg-00000000000000010" }
}

override_resource {
  override_during = plan
  target          = aws_subnet.curation_consumer
  values          = { id = "subnet-00000000000000032" }
}

override_resource {
  override_during = plan
  target          = aws_security_group.outbox_relay
  values          = { id = "sg-00000000000000011" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.curation_consumer[0]
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-consumer" }
}

override_resource {
  override_during = plan
  target          = aws_lambda_function.curation_outbox_relay[0]
  values          = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-curation-outbox-relay" }
}
