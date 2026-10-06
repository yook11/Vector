resource "aws_iam_role" "investigator" {
  name = "${local.prefix}-readonly"
  path = "/vector-test/bastion-automation/"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRole", Principal = { AWS = "arn:aws:iam::${local.account}:root" },
    Condition = { ArnLike = { "aws:PrincipalArn" = "arn:aws:iam::${local.account}:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_WorkloadAdministrator_*" } }
  }] })
}
resource "aws_iam_role_policy" "investigator" {
  name = "assume-operations"
  role = aws_iam_role.investigator.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.operations.arn },
    { Effect = "Allow", Action = "sqs:GetQueueAttributes", Resource = [aws_sqs_queue.source.arn, aws_sqs_queue.dlq.arn] }
  ] })
}
resource "aws_iam_role" "operations" {
  name                 = "${local.prefix}-operations"
  path                 = "/vector-test/bastion-automation/"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { AWS = aws_iam_role.investigator.arn }
  }] })
}
resource "aws_iam_role_policy" "tunnel" {
  name = "fixed-sqs-tunnel"
  role = aws_iam_role.operations.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect = "Allow", Action = "ssm:StartSession", Resource = "${local.ec2_arn}:instance/*"
      Condition = {
        BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
        StringEquals = { "ssm:resourceTag/vector:session-purpose" = "sqs-redrive", "ssm:resourceTag/vector:managed-bastion" = local.prefix }
      }
    },
    { Effect = "Allow", Action = "ssm:StartSession", Resource = aws_ssm_document.tunnel.arn },
    { Effect = "Allow", Action = "ssmmessages:OpenDataChannel", Resource = "arn:aws:ssm:${local.region}:${local.account}:session/$${aws:userid}-*" },
    {
      Effect    = "Allow", Action = "ssm:TerminateSession", Resource = "arn:aws:ssm:${local.region}:${local.account}:session/*"
      Condition = { StringEquals = { "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}" } }
    },
    { Effect = "Allow", Action = ["sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask", "sqs:ListMessageMoveTasks", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.dlq.arn },
    { Effect = "Allow", Action = ["sqs:SendMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.source.arn }
  ] })
}
