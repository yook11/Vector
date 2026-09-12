mock_provider "aws" {
  override_during = plan
  mock_resource "aws_vpc" { override_during = plan }
  mock_resource "aws_subnet" { override_during = plan }
  mock_resource "aws_security_group" { override_during = plan }
  mock_resource "aws_internet_gateway" { override_during = plan }
  mock_resource "aws_vpc_endpoint" { override_during = plan }
  mock_resource "aws_route_table" {
    override_during = plan
    defaults        = { route = [] }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address     = "smoke.example.ap-northeast-1.rds.amazonaws.com"
      resource_id = "db-SMOKE"
      master_user_secret = [{
        secret_arn = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:rds!db-smoke"
        kms_key_id = "mock", secret_status = "active"
      }]
    }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/vector-test/runtime/mock" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:ap-northeast-1:123456789012:vector-test-contract-embedding" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:vector-test-contract-embedding" }
  }
}
variables {
  expected_account_id   = "123456789012"
  aws_profile           = "aws-test-admin"
  run_id                = "contract"
  backend_image_digest  = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  proxy_image_digest    = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  source_revision       = "cccccccccccccccccccccccccccccccccccccccc"
  gemini_parameter_path = "/vector-test/embedding-consumer/gemini-api-key"
}
run "reject_malformed_account" {
  command = plan
  variables { expected_account_id = "invalid-account" }
  expect_failures = [var.expected_account_id]
}
run "reject_image_tag" {
  command = plan
  variables { backend_image_digest = "latest" }
  expect_failures = [var.backend_image_digest]
}
run "reject_missing_proxy_digest" {
  command = plan
  variables { proxy_image_digest = "" }
  expect_failures = [var.proxy_image_digest]
}
run "reject_state_path_in_run_id" {
  command = plan
  variables { run_id = "../bootstrap" }
  expect_failures = [var.run_id]
}
run "network_and_runtime_contract" {
  command = plan
  assert {
    condition = (
      aws_instance.runtime["proxy"].associate_public_ip_address &&
      alltrue([for instance in aws_instance.runtime : instance.instance_type == "t4g.small"]) &&
      !aws_instance.runtime["runner"].associate_public_ip_address &&
      !aws_db_instance.smoke.publicly_accessible &&
      aws_instance.runtime["proxy"].source_dest_check &&
      alltrue([for subnet in aws_subnet.smoke : !subnet.map_public_ip_on_launch]) &&
      length(aws_route_table.smoke["private"].route) == 0 &&
      aws_route.internet.gateway_id == aws_internet_gateway.proxy.id &&
      alltrue([for rule in aws_vpc_security_group_ingress_rule.private : rule.from_port != 22 && rule.cidr_ipv4 == null])
    )
    error_message = "公開IPとインターネット経路はプロキシ専用とし、NAT・SSH公開を作りません。"
  }
  assert {
    condition = (
      aws_vpc_endpoint.ssm.private_dns_enabled &&
      aws_vpc_endpoint.ssm.vpc_endpoint_type == "Interface" &&
      length(aws_vpc_endpoint.ssm.subnet_ids) == 1 &&
      aws_vpc_endpoint.ssm.service_name == "com.amazonaws.ap-northeast-1.ssm" &&
      aws_vpc_endpoint.ssmmessages.service_name == "com.amazonaws.ap-northeast-1.ssmmessages" &&
      aws_vpc_endpoint.ssmmessages.private_dns_enabled &&
      aws_vpc_endpoint.ssmmessages.vpc_endpoint_type == "Interface" &&
      aws_vpc_endpoint.ssmmessages.subnet_ids == aws_vpc_endpoint.ssm.subnet_ids &&
      aws_vpc_endpoint.ssmmessages.security_group_ids == aws_vpc_endpoint.ssm.security_group_ids &&
      output.resources.ssmmessages_endpoint == aws_vpc_endpoint.ssmmessages.id &&
      strcontains(local.no_proxy, "ssmmessages.ap-northeast-1.amazonaws.com") &&
      !contains(local.runner_domains, "ssmmessages.ap-northeast-1.amazonaws.com") &&
      strcontains(local.startup.runner, "UnsetEnvironment=http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy") &&
      !strcontains(local.startup.runner, "amazon-ssm-agent.service.d/proxy.conf") &&
      strcontains(local.no_proxy, "ssm.ap-northeast-1.amazonaws.com") &&
      strcontains(local.no_proxy, "169.254.169.254") &&
      strcontains(local.squid_config, "http_access allow src_embedding dst_embedding") &&
      strcontains(local.squid_config, "acl dst_embedding dstdomain generativelanguage.googleapis.com\n") &&
      strcontains(local.squid_config, "prod-ap-northeast-1-starport-layer-bucket.s3.ap-northeast-1.amazonaws.com") &&
      !strcontains(local.squid_config, "http_access allow src_embedding dst_runner")
    )
    error_message = "SSMの非公開経路と、Lambda・runner別のプロキシ許可先を維持します。"
  }
  assert {
    condition = (
      !strcontains(module.runtime_policy.policies.lambda, "secretsmanager:") &&
      !strcontains(module.runtime_policy.policies.lambda, "/vector\"") &&
      strcontains(module.runtime_policy.policies.lambda, "db-SMOKE/vector_app") &&
      strcontains(module.runtime_policy.policies.lambda, "lambda:SourceFunctionArn") &&
      !strcontains(module.runtime_policy.policies.proxy, "ssm:GetParameter") &&
      !strcontains(module.runtime_policy.policies.proxy, "rds-db:") &&
      !strcontains(module.runtime_policy.policies.proxy, "secretsmanager:")
    )
    error_message = "Lambdaへ準備権限を渡さず、プロキシへDBやAIキーへの権限を渡しません。"
  }
  assert {
    condition = (
      endswith(aws_lambda_function.embedding.image_uri, var.backend_image_digest) &&
      (aws_lambda_function.embedding.reserved_concurrent_executions == null || aws_lambda_function.embedding.reserved_concurrent_executions == -1) &&
      aws_lambda_function.embedding.timeout == 120 &&
      aws_sqs_queue.embedding.visibility_timeout_seconds == 720 &&
      aws_sqs_queue.embedding.message_retention_seconds == 345600 &&
      aws_sqs_queue.embedding.sqs_managed_sse_enabled &&
      aws_lambda_event_source_mapping.embedding.batch_size == 1 &&
      aws_lambda_event_source_mapping.embedding.maximum_batching_window_in_seconds == 0 &&
      aws_lambda_event_source_mapping.embedding.scaling_config[0].maximum_concurrency == 2 &&
      contains(aws_lambda_event_source_mapping.embedding.function_response_types, "ReportBatchItemFailures")
    )
    error_message = "固定digestとSQS配信設定を維持し、予約同時実行は設定しません。"
  }
  assert {
    condition = (
      !aws_db_instance.smoke.deletion_protection &&
      aws_db_instance.smoke.skip_final_snapshot &&
      aws_db_instance.smoke.backup_retention_period == 0 &&
      aws_db_instance.smoke.delete_automated_backups &&
      aws_db_instance.smoke.manage_master_user_password &&
      aws_db_instance.smoke.iam_database_authentication_enabled &&
      alltrue([for instance in aws_instance.runtime : instance.root_block_device[0].delete_on_termination && instance.root_block_device[0].encrypted && instance.metadata_options[0].http_tokens == "required"]) &&
      alltrue([for group in aws_cloudwatch_log_group.runtime : group.retention_in_days == 7])
    )
    error_message = "秘密値を取得せず、削除時にDBバックアップやEC2ルートボリュームを残しません。"
  }
  assert {
    condition = (
      output.run.state_key == "smoke/contract/terraform.tfstate" &&
      output.run.state_bucket == "vector-test-tfstate-123456789012" &&
      strcontains(file("${path.module}/versions.tf"), "allowed_account_ids") &&
      can(regex("use_lockfile\\s*=\\s*true", file("${path.module}/versions.tf")))
    )
    error_message = "試験stateを分離し、接続先制限とS3ロックを維持します。"
  }
}

run "tags_paths_logs_and_cleanup" {
  command = plan
  assert {
    condition = alltrue([for tags in concat(
      [aws_vpc.smoke.tags, aws_internet_gateway.proxy.tags, aws_vpc_endpoint.ssm.tags, aws_vpc_endpoint.ssmmessages.tags, aws_cloudwatch_log_group.database.tags,
        aws_db_instance.smoke.tags, aws_db_subnet_group.smoke.tags, aws_db_parameter_group.smoke.tags,
      aws_sqs_queue.embedding.tags, aws_lambda_function.embedding.tags, aws_lambda_event_source_mapping.embedding.tags],
      [for resource in aws_subnet.smoke : resource.tags],
      [for resource in aws_route_table.smoke : resource.tags],
      [for resource in aws_security_group.smoke : resource.tags],
      [for resource in aws_vpc_security_group_ingress_rule.private : resource.tags],
      [for resource in aws_vpc_security_group_egress_rule.private : resource.tags],
      [for resource in aws_vpc_security_group_egress_rule.proxy_internet : resource.tags],
      [for resource in aws_instance.runtime : resource.tags],
      [for resource in aws_iam_role.runtime : resource.tags],
      [for resource in aws_iam_instance_profile.runtime : resource.tags],
      [for resource in aws_cloudwatch_log_group.runtime : resource.tags]
    ) : tags.Project == "vector-test" && tags.Lifecycle == "smoke" && tags.RunId == var.run_id])
    error_message = "作成・管理権限と削除確認に必要なProject/Lifecycle/RunIdを全設備へ付与します。"
  }
  assert {
    condition = (
      can(regex("default_tags\\s*\\{\\s*tags\\s*=\\s*local\\.tags\\s*\\}", file("${path.module}/versions.tf"))) &&
      local.tags.Project == "vector-test" && local.tags.Lifecycle == "smoke" && local.tags.RunId == var.run_id &&
      alltrue([for kind, instance in aws_instance.runtime :
        length(instance.root_block_device[0].tags) == 1 &&
        instance.root_block_device[0].tags["Name"] == "vector-test-${var.run_id}-${kind}"
      ])
    )
    error_message = "EBSの必須タグはdefault_tagsから起動時に付与し、起動後のタグ更新はNameだけに限定します。"
  }
  assert {
    condition = (
      alltrue([for kind, role in aws_iam_role.runtime :
        role.path == "/vector-test/runtime/" &&
        role.name == "vector-test-${var.run_id}-${kind}" &&
        role.permissions_boundary == "arn:aws:iam::${var.expected_account_id}:policy/vector-test/bootstrap/vector-test-${kind}-boundary"
      ]) &&
      alltrue([for kind, profile in aws_iam_instance_profile.runtime : profile.path == "/vector-test/runtime/" && profile.role == aws_iam_role.runtime[kind].name])
    )
    error_message = "構築ロールのPassRole条件と、実行ロールのパス・名前・権限境界を一致させます。"
  }
  assert {
    condition = alltrue([for kind, policy in module.runtime_policy.policies :
      one([for statement in jsondecode(policy).Statement : statement if statement.Sid == "Logs"]).Resource == "arn:aws:logs:ap-northeast-1:${var.expected_account_id}:log-group:${aws_cloudwatch_log_group.runtime[kind].name}:*" &&
      aws_cloudwatch_log_group.runtime[kind].name == "/vector-test/vector-test-${var.run_id}/${kind}"
    ])
    error_message = "実際のロググループ名と実行ロールの書込先ARNを一致させます。"
  }
  assert {
    condition = (
      terraform_data.lambda_eni_cleanup.input.expected_account_id == var.expected_account_id &&
      terraform_data.lambda_eni_cleanup.input.aws_profile == var.aws_profile &&
      terraform_data.lambda_eni_cleanup.input.subnet_id == aws_subnet.smoke["lambda"].id &&
      terraform_data.lambda_eni_cleanup.input.security_group_id == aws_security_group.smoke["lambda"].id &&
      strcontains(file("${path.module}/cleanup.tf"), "depends_on = [aws_iam_role_policy.runtime]") &&
      strcontains(file("${path.module}/embedding.tf"), "terraform_data.lambda_eni_cleanup,") &&
      !strcontains(file("${path.module}/cleanup.tf"), "on_failure")
    )
    error_message = "ENI待機は対象ネットワークを参照し、失敗時にはIAM権限の削除を止めます。"
  }
}

run "database_log_export_and_observation" {
  command = plan
  assert {
    condition = (
      aws_db_instance.smoke.enabled_cloudwatch_logs_exports == toset(["postgresql"]) &&
      aws_cloudwatch_log_group.database.name == "/aws/rds/instance/vector-test-${var.run_id}/postgresql" &&
      aws_cloudwatch_log_group.database.retention_in_days == 7 &&
      aws_cloudwatch_log_group.database.skip_destroy != true &&
      strcontains(file("${path.module}/database.tf"), "depends_on = [aws_cloudwatch_log_group.database]") &&
      output.execution.log_groups.database == aws_cloudwatch_log_group.database.name &&
      output.resources.log_groups.database == aws_cloudwatch_log_group.database.name
    )
    error_message = "RDSログは7日保持の試験用ロググループへ転送し、回収・削除対象に含めてRDS削除後に削除します。"
  }
  assert {
    condition = (
      aws_db_instance.smoke.backup_retention_period == 0 &&
      aws_db_instance.smoke.skip_final_snapshot &&
      aws_lambda_function.embedding.tracing_config[0].mode == "PassThrough"
    )
    error_message = "再生成できる試験DBのバックアップは保持せず、LambdaはX-RayのActive tracingを使用しません。"
  }
}
