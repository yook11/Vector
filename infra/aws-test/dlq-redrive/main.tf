terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region              = "ap-northeast-1"
  profile             = var.aws_profile
  allowed_account_ids = [local.account_id]
  default_tags {
    tags = { Project = "vector-test", Lifecycle = "dlq-redrive", RunId = var.run_id, ManagedBy = "terraform" }
  }
}

variable "aws_profile" {
  type    = string
  default = "vector-test-admin"
}

variable "run_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,23}$", var.run_id))
    error_message = "run_idは英小文字・数字・ハイフンの1〜24文字で指定してください。"
  }
}

variable "phase" {
  type    = string
  default = "seed"
  validation {
    condition     = contains(["seed", "verify"], var.phase)
    error_message = "phaseはseedまたはverifyです。"
  }
}

locals {
  account_id = "733360597472"
  prefix     = "vector-test-dlq-${var.run_id}"
  cases      = toset(["current", "queue-called-via", "queue-via-service", "control"])
  source_arns = {
    for name in local.cases : name => "arn:aws:sqs:ap-northeast-1:${local.account_id}:${local.prefix}-${name}"
  }
  dlq_arns = { for name, arn in local.source_arns : name => "${arn}-dlq" }
  # 公開経路だけの試験では実在しないVPCE IDを比較値にする。
  excluded_endpoint = var.enable_private_path ? aws_vpc_endpoint.sqs[0].id : "vpce-00000000000000000"
}

resource "aws_sqs_queue" "dlq" {
  for_each                  = local.cases
  name                      = "${local.prefix}-${each.key}-dlq"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 3600
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [local.source_arns[each.key]]
  })
}

resource "aws_sqs_queue" "source" {
  for_each                   = local.cases
  name                       = "${local.prefix}-${each.key}"
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = 3600
  visibility_timeout_seconds = 1
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 1
  })
}

resource "aws_sqs_queue" "outside" {
  name                      = "${local.prefix}-outside"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 3600
}

resource "aws_sqs_queue_policy" "source" {
  for_each  = local.cases
  queue_url = aws_sqs_queue.source[each.key].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = local.source_arns[each.key]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
      ], var.phase == "verify" && each.key != "control" ? [{
        Sid       = "DenySendOutsideEndpoint"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:SendMessage"
        Resource  = local.source_arns[each.key]
        Condition = merge(
          { StringNotEquals = { "aws:sourceVpce" = local.excluded_endpoint } },
          each.key == "queue-via-service" ? {
            BoolIfExists = { "aws:ViaAWSService" = "false" }
            } : {
            StringNotEqualsIfExists = { "aws:CalledViaLast" = "sqs.amazonaws.com" }
          }
        )
    }] : [])
  })
}

resource "aws_sqs_queue_policy" "dlq" {
  for_each  = local.cases
  queue_url = aws_sqs_queue.dlq[each.key].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = local.dlq_arns[each.key]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_iam_role" "operations" {
  for_each             = local.cases
  name                 = "${local.prefix}-${each.key}"
  path                 = "/vector-test/dlq-redrive/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
      Condition = {
        ArnLike = {
          "aws:PrincipalArn" = var.enable_private_path && each.key == "queue-called-via" ? aws_iam_role.investigator[0].arn : "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_WorkloadAdministrator_*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "operations" {
  for_each = local.cases
  name     = "dlq-redrive"
  role     = aws_iam_role.operations[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RedriveTestDlq"
        Effect = "Allow"
        Action = [
          "sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask", "sqs:ListMessageMoveTasks",
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
        ]
        Resource = [local.dlq_arns[each.key]]
      },
      merge({
        Sid      = "SendToTestQueue"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = [local.source_arns[each.key]]
        }, each.key == "current" ? {
        Condition = { StringEquals = { "aws:CalledViaLast" = "sqs.amazonaws.com" } }
      } : {}),
      {
        Sid      = "ObserveTestQueues"
        Effect   = "Allow"
        Action   = ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource = [local.source_arns[each.key], local.dlq_arns[each.key]]
      }
    ]
  })
}

output "fixture" {
  value = {
    account_id  = local.account_id
    phase       = var.phase
    prefix      = local.prefix
    outside_url = aws_sqs_queue.outside.url
    private_connection = var.enable_private_path ? {
      instance_id   = one(aws_instance.bastion[*].id)
      document_name = aws_ssm_document.sqs_tunnel[0].name
      endpoint_id   = aws_vpc_endpoint.sqs[0].id
      vpc_id        = aws_vpc.test[0].id
    } : null
    investigator_arn = var.enable_private_path ? aws_iam_role.investigator[0].arn : null
    cases = {
      for name in local.cases : name => {
        role_arn   = aws_iam_role.operations[name].arn
        role_name  = aws_iam_role.operations[name].name
        source_url = aws_sqs_queue.source[name].url
        source_arn = local.source_arns[name]
        dlq_url    = aws_sqs_queue.dlq[name].url
        dlq_arn    = local.dlq_arns[name]
      }
    }
  }
}
