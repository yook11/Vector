locals {
  operations_role_arn = "arn:aws:iam::${local.account_id}:role/vector-operations"
  deploy_sso_role     = "arn:aws:iam::${local.account_id}:role/${local.sso_path}/AWSReservedSSO_VectorDeploy_*"
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
      Sid       = "VectorDeployOnly"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
      Condition = { ArnLike = { "aws:PrincipalArn" = local.deploy_sso_role } }
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
        Condition = {
          StringEquals = { "aws:CalledViaLast" = "sqs.amazonaws.com" }
        }
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

output "deploy_operations_assume_statement" {
  description = "VectorDeployの既存inline policyへ追加するstatementであり、全体を置換しない。"
  value = {
    Sid      = "AssumeVectorOperations"
    Effect   = "Allow"
    Action   = "sts:AssumeRole"
    Resource = local.operations_role_arn
  }
}
