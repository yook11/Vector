terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "run_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,23}$", var.run_id))
    error_message = "run_idは英小文字・数字・ハイフンの1〜24文字です。"
  }
}

locals {
  account = "733360597472"
  region  = "ap-northeast-1"
  prefix  = "vector-test-auto-${var.run_id}"
  purpose = "sqs-redrive"
  tags    = { RunId = var.run_id, "vector:session-purpose" = local.purpose }
  ec2_arn = "arn:aws:ec2:${local.region}:${local.account}"
}

provider "aws" {
  profile             = "vector-test-admin"
  region              = local.region
  allowed_account_ids = [local.account]
  default_tags {
    tags = { Project = "vector-test", Lifecycle = "bastion-automation", RunId = var.run_id, ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_vpc" "test" {
  cidr_block           = "10.95.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.prefix }
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == local.account
      error_message = "テストアカウントだけで実行してください。"
    }
  }
}

resource "aws_subnet" "test" {
  lifecycle { create_before_destroy = true }
  for_each          = { allowed = "10.95.0.64/27", outside = "10.95.0.96/27" }
  vpc_id            = aws_vpc.test.id
  availability_zone = "ap-northeast-1c"
  cidr_block        = each.value
}

resource "aws_security_group" "test" {
  for_each    = toset(["bastion", "endpoints", "outside"])
  name        = "${local.prefix}-${each.key}"
  description = "Isolated EC2 lifecycle probe"
  vpc_id      = aws_vpc.test.id
}

resource "aws_vpc_security_group_egress_rule" "bastion" {
  security_group_id            = aws_security_group.test["bastion"].id
  referenced_security_group_id = aws_security_group.test["endpoints"].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "endpoint" {
  security_group_id            = aws_security_group.test["endpoints"].id
  referenced_security_group_id = aws_security_group.test["bastion"].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "test" {
  for_each            = toset(["ssm", "ssmmessages", "sqs"])
  vpc_id              = aws_vpc.test.id
  service_name        = "com.amazonaws.${local.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.test["allowed"].id]
  security_group_ids  = [aws_security_group.test["endpoints"].id]
  private_dns_enabled = true
  policy = each.key == "sqs" ? jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Principal = { AWS = aws_iam_role.operations.arn },
    Action = ["sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask", "sqs:ListMessageMoveTasks", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.dlq.arn },
    { Effect = "Allow", Principal = { AWS = aws_iam_role.operations.arn }, Action = ["sqs:SendMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.source.arn }
  ] }) : null
}

resource "aws_network_interface" "bastion" {
  subnet_id       = aws_subnet.test["allowed"].id
  security_groups = [aws_security_group.test["bastion"].id]
  tags            = { Name = local.prefix }
}
module "bastion" {
  source               = "../../modules/bastion-automation"
  name_prefix          = local.prefix
  account_id           = local.account
  role_path            = "/vector-test/bastion-automation/"
  operations_role_name = aws_iam_role.operations.name
  network = {
    vpc_id            = aws_vpc.test.id, subnet_id = aws_subnet.test["allowed"].id,
    security_group_id = aws_security_group.test["bastion"].id, network_interface_id = aws_network_interface.bastion.id
  }
}
resource "aws_sqs_queue" "dlq" { name = "${local.prefix}-dlq" }
resource "aws_sqs_queue" "source" {
  name                    = "${local.prefix}-source"
  sqs_managed_sse_enabled = true
  redrive_policy          = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dlq.arn, maxReceiveCount = 2 })
}
resource "aws_sqs_queue_policy" "source" {
  queue_url = aws_sqs_queue.source.url
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = aws_sqs_queue.source.arn, Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Effect = "Deny", Principal = "*", Action = "sqs:SendMessage", Resource = aws_sqs_queue.source.arn,
    Condition = { StringNotEquals = { "aws:sourceVpce" = aws_vpc_endpoint.test["sqs"].id }, StringNotEqualsIfExists = { "aws:CalledViaLast" = "sqs.amazonaws.com" } } }
  ] })
}
resource "aws_ssm_document" "tunnel" {
  name            = "${local.prefix}-sqs-tunnel"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "1.0", description = "Fixed SQS tunnel for lifecycle test", sessionType = "Port"
    parameters = { localPortNumber = {
      type           = "String", default = "18443"
      allowedPattern = "^([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])$"
    } }
    properties = { host = "sqs.ap-northeast-1.amazonaws.com", portNumber = "443", localPortNumber = "{{ localPortNumber }}", type = "LocalPortForwarding" }
  })
}

output "fixture" {
  value = {
    account_id       = local.account, region = local.region, run_id = var.run_id, prefix = local.prefix,
    investigator_arn = aws_iam_role.investigator.arn, operations_arn = aws_iam_role.operations.arn,
    operations_name  = aws_iam_role.operations.name, configuration = module.bastion.configuration,
    tunnel_document  = aws_ssm_document.tunnel.name, source_url = aws_sqs_queue.source.url,
    source_arn       = aws_sqs_queue.source.arn, dlq_url = aws_sqs_queue.dlq.url, dlq_arn = aws_sqs_queue.dlq.arn,
    endpoint_id      = aws_vpc_endpoint.test["sqs"].id
  }
}

resource "aws_sqs_queue" "outside" { name = "${local.prefix}-outside" }
output "outside_queue_url" { value = aws_sqs_queue.outside.url }
