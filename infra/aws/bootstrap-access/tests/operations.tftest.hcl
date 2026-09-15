mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_WorkloadAdministrator_abc123/admin"
    }
  }
}

variables {
  expected_account_id = "123456789012"
  hosted_zone_id      = "Z0123456789EXAMPLE"
}

run "operations_scope" {
  command = plan
  assert {
    condition = (
      jsondecode(aws_iam_role.operations.assume_role_policy).Statement[0].Condition.ArnLike["aws:PrincipalArn"] ==
      "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_VectorDeploy_*" &&
      aws_iam_role.operations.max_session_duration == 3600 &&
      output.deploy_operations_assume_statement.Resource == output.operations_role_arn
    )
    error_message = "デプロイSSOから1時間の運用セッションだけを発行する。"
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
      jsondecode(aws_iam_role_policy.operations.policy).Statement[1].Condition.StringEquals["aws:CalledViaLast"] == "sqs.amazonaws.com"
    )
    error_message = "受信・削除はDLQだけ、送信は元キューへのSQS経由だけに限定する。"
  }
}
