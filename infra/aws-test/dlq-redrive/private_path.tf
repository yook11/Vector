variable "enable_bastion" {
  type        = bool
  default     = true
  description = "VPCを維持して一時EC2の撤去・再作成を確認する。"
}

variable "enable_private_path" {
  type        = bool
  default     = false
  description = "試験専用のVPC・SSM踏み台・SQSエンドポイントを作成する。"
}

resource "aws_vpc" "test" {
  count                = var.enable_private_path ? 1 : 0
  cidr_block           = "10.93.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.prefix }
}

resource "aws_subnet" "test" {
  count             = var.enable_private_path ? 1 : 0
  vpc_id            = aws_vpc.test[0].id
  availability_zone = "ap-northeast-1a"
  cidr_block        = "10.93.0.0/27"
}

resource "aws_security_group" "bastion" {
  count       = var.enable_private_path ? 1 : 0
  name        = "${local.prefix}-bastion"
  description = "SSM tunnel only; no inbound access"
  vpc_id      = aws_vpc.test[0].id
}

resource "aws_security_group" "endpoints" {
  count       = var.enable_private_path ? 1 : 0
  name        = "${local.prefix}-endpoints"
  description = "HTTPS from the test bastion only"
  vpc_id      = aws_vpc.test[0].id
}

resource "aws_vpc_security_group_egress_rule" "bastion_https" {
  count                        = var.enable_private_path ? 1 : 0
  security_group_id            = aws_security_group.bastion[0].id
  referenced_security_group_id = aws_security_group.endpoints[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "endpoint_https" {
  count                        = var.enable_private_path ? 1 : 0
  security_group_id            = aws_security_group.endpoints[0].id
  referenced_security_group_id = aws_security_group.bastion[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "ssm" {
  for_each            = var.enable_private_path ? toset(["ssm", "ssmmessages"]) : toset([])
  vpc_id              = aws_vpc.test[0].id
  service_name        = "com.amazonaws.ap-northeast-1.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.test[0].id]
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "sqs" {
  count               = var.enable_private_path ? 1 : 0
  vpc_id              = aws_vpc.test[0].id
  service_name        = "com.amazonaws.ap-northeast-1.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.test[0].id]
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = flatten([for name in local.cases : [
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.operations[name].arn }
        Action = [
          "sqs:StartMessageMoveTask", "sqs:CancelMessageMoveTask", "sqs:ListMessageMoveTasks",
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl",
        ]
        Resource = [local.dlq_arns[name]]
      },
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.operations[name].arn }
        Action    = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource  = [local.source_arns[name]]
      }
    ]])
  })
}

resource "aws_iam_role" "bastion" {
  count = var.enable_private_path ? 1 : 0
  name  = "${local.prefix}-bastion"
  path  = "/vector-test/dlq-redrive/"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  count      = var.enable_private_path ? 1 : 0
  role       = aws_iam_role.bastion[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  count = var.enable_private_path ? 1 : 0
  name  = "${local.prefix}-bastion"
  role  = aws_iam_role.bastion[0].name
}

data "aws_ssm_parameter" "bastion_ami" {
  count = var.enable_private_path ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "bastion" {
  count                       = var.enable_private_path && var.enable_bastion ? 1 : 0
  ami                         = data.aws_ssm_parameter.bastion_ami[0].value
  instance_type               = "t4g.nano"
  subnet_id                   = aws_subnet.test[0].id
  vpc_security_group_ids      = [aws_security_group.bastion[0].id]
  associate_public_ip_address = false
  iam_instance_profile        = aws_iam_instance_profile.bastion[0].name
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }
  root_block_device {
    encrypted             = true
    delete_on_termination = true
    volume_type           = "gp3"
    volume_size           = 8
  }
  tags = { Name = "${local.prefix}-bastion", "vector:session-purpose" = "sqs-redrive" }
  depends_on = [
    aws_vpc_endpoint.ssm, aws_iam_role_policy_attachment.bastion_ssm,
    aws_vpc_security_group_egress_rule.bastion_https, aws_vpc_security_group_ingress_rule.endpoint_https,
  ]
}

resource "aws_ssm_document" "sqs_tunnel" {
  count           = var.enable_private_path ? 1 : 0
  name            = "${local.prefix}-sqs-tunnel"
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
  for_each = var.enable_private_path ? local.cases : toset([])
  name     = "sqs-tunnel"
  role     = aws_iam_role.operations[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ssm:StartSession"
        Resource = "arn:aws:ec2:ap-northeast-1:${local.account_id}:instance/*"
        Condition = {
          BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
          StringEquals = { "ssm:resourceTag/vector:session-purpose" = "sqs-redrive" }
        }
      },
      {
        Effect   = "Allow"
        Action   = "ssm:StartSession"
        Resource = aws_ssm_document.sqs_tunnel[0].arn
      },
      {
        Effect   = "Allow"
        Action   = "ssmmessages:OpenDataChannel"
        Resource = "arn:aws:ssm:ap-northeast-1:${local.account_id}:session/$${aws:userid}-*"
      },
      {
        Effect   = "Allow"
        Action   = "ssm:TerminateSession"
        Resource = "arn:aws:ssm:ap-northeast-1:${local.account_id}:session/*"
        Condition = {
          StringEquals = {
            "ssm:resourceTag/aws:ssmmessages:session-id" = "$${aws:userid}"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role" "investigator" {
  count = var.enable_private_path ? 1 : 0
  name  = "${local.prefix}-investigator"
  path  = "/vector-test/dlq-redrive/"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { AWS = "arn:aws:iam::${local.account_id}:root" },
      Condition = { ArnLike = {
        "aws:PrincipalArn" = "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/ap-northeast-1/AWSReservedSSO_WorkloadAdministrator_*"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "investigator" {
  count = var.enable_private_path ? 1 : 0
  name  = "observe-and-assume"
  role  = aws_iam_role.investigator[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow", Action = "sts:AssumeRole",
        Resource = aws_iam_role.operations["queue-called-via"].arn
      },
      {
        Effect   = "Allow", Action = ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
        Resource = [local.source_arns["queue-called-via"], local.dlq_arns["queue-called-via"]]
      }
    ]
  })
}
