locals {
  operations_role_arn = "arn:aws:iam::${local.account_id}:role/vector-operations"
  readonly_sso_role   = "arn:aws:iam::${local.account_id}:role/${local.sso_path}/AWSReservedSSO_ReadOnly_*"
  operations_queue_names = [
    "vector-source-acquisition",
    "vector-article-completion",
    "vector-article-curation",
    "vector-article-assessment",
    "vector-article-embedding",
  ]
  operations_queue_arns = [
    for name in local.operations_queue_names : "arn:aws:sqs:ap-northeast-1:${local.account_id}:${name}"
  ]
  operations_dlq_arns = [for arn in local.operations_queue_arns : "${arn}-dlq"]
}

resource "aws_iam_role" "operations" {
  name                 = "vector-operations"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ReadOnlyToOperations"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
      Condition = { ArnLike = { "aws:PrincipalArn" = local.readonly_sso_role } }
    }]
  })
  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        data.aws_caller_identity.current.account_id == local.account_id &&
        can(regex("^arn:aws:sts::${local.account_id}:assumed-role/AWSReservedSSO_WorkloadAdministrator_[0-9a-fA-F]+/.+$", data.aws_caller_identity.current.arn))
      )
      error_message = "運用ロールは対象アカウントのWorkloadAdministratorから管理してください。"
    }
  }
}

resource "aws_iam_role_policy" "operations" {
  name = "pipeline-dlq-redrive"
  role = aws_iam_role.operations.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RedrivePipelineDlqs"
        Effect = "Allow"
        Action = [
          "sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask",
          "sqs:ListMessageMoveTasks", "sqs:ReceiveMessage",
          "sqs:DeleteMessage", "sqs:GetQueueAttributes",
        ]
        Resource = local.operations_dlq_arns
      },
      {
        Sid      = "SendToPipelineQueues"
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = local.operations_queue_arns
      },
      {
        Sid      = "ObservePipelineQueues"
        Effect   = "Allow"
        Action   = ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource = concat(local.operations_queue_arns, local.operations_dlq_arns)
      },
    ]
  })
}

output "operations_role_arn" {
  value = local.operations_role_arn
}

output "readonly_operations_assume_statement" {
  description = "ReadOnlyの既存inline policyへ追加するstatementであり、全体を置換しない。"
  value = {
    Sid      = "AssumeVectorOperations"
    Effect   = "Allow"
    Action   = "sts:AssumeRole"
    Resource = local.operations_role_arn
  }
}

resource "aws_ssm_document" "sqs_tunnel" {
  name            = "vector-sqs-tunnel"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "1.0"
    description   = "Forward only to the Tokyo SQS endpoint"
    sessionType   = "Port"
    parameters = {
      localPortNumber = {
        type           = "String"
        default        = "18443"
        allowedPattern = "^([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])$"
      }
    }
    properties = {
      host            = "sqs.ap-northeast-1.amazonaws.com"
      portNumber      = "443"
      localPortNumber = "{{ localPortNumber }}"
      type            = "LocalPortForwarding"
    }
  })
}

resource "aws_iam_role_policy" "operations_tunnel" {
  name = "sqs-tunnel"
  role = aws_iam_role.operations.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartTaggedBastionSession"
        Effect   = "Allow"
        Action   = "ssm:StartSession"
        Resource = "arn:aws:ec2:ap-northeast-1:${local.account_id}:instance/*"
        Condition = {
          StringEquals = { "ssm:resourceTag/vector:session-purpose" = "sqs-redrive" }
          BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
        }
      },
      {
        Sid      = "UseFixedSqsDocument"
        Effect   = "Allow"
        Action   = "ssm:StartSession"
        Resource = aws_ssm_document.sqs_tunnel.arn
      },
      {
        Sid      = "OpenOwnSessionChannel"
        Effect   = "Allow"
        Action   = "ssmmessages:OpenDataChannel"
        Resource = "arn:aws:ssm:ap-northeast-1:${local.account_id}:session/$${aws:userid}-*"
      },
      {
        Sid      = "TerminateOwnSession"
        Effect   = "Allow"
        Action   = "ssm:TerminateSession"
        Resource = "arn:aws:ssm:ap-northeast-1:${local.account_id}:session/*"
        Condition = {
          StringEquals = { "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" }
        }
      }
    ]
  })
}

output "sqs_tunnel_document_name" {
  value = aws_ssm_document.sqs_tunnel.name
}
