locals {
  outbox_queue_stages = toset([
    "completion", "curation", "assessment", "embedding",
  ])
  outbox_relay_eni_actions = [
    "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets",
    "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses",
  ]
}

resource "aws_sqs_queue" "outbox" {
  for_each = local.outbox_queue_stages

  name                       = "${var.name_prefix}-article-${each.key}"
  fifo_queue                 = false
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = contains(["embedding", "assessment", "curation"], each.key) ? 345600 : 1209600
  visibility_timeout_seconds = contains(["embedding", "assessment", "curation"], each.key) ? 720 : 3600
  redrive_policy = jsonencode({
    deadLetterTargetArn = { embedding = aws_sqs_queue.embedding_dlq.arn, assessment = aws_sqs_queue.assessment_dlq.arn, curation = aws_sqs_queue.curation_dlq.arn, completion = aws_sqs_queue.completion_dlq.arn }[each.key]
    maxReceiveCount     = 5
  })
}

resource "aws_security_group" "outbox_relay" {
  name        = local.outbox_relay_name
  description = "Outbox relay Lambda access to RDS and SQS only."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "outbox_sqs_endpoint" {
  name        = "${var.name_prefix}-outbox-sqs-vpce"
  description = "SQS private endpoint for the outbox relay."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_outbox_relay" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.outbox_relay.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "outbox_relay_to_rds" {
  security_group_id            = aws_security_group.outbox_relay.id
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "sqs_from_outbox_relay" {
  security_group_id            = aws_security_group.outbox_sqs_endpoint.id
  referenced_security_group_id = aws_security_group.outbox_relay.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "outbox_relay_to_sqs" {
  security_group_id            = aws_security_group.outbox_relay.id
  referenced_security_group_id = aws_security_group.outbox_sqs_endpoint.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "outbox_sqs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app["api"].id]
  security_group_ids  = [aws_security_group.outbox_sqs_endpoint.id]
  private_dns_enabled = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.source_dispatch.arn }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.source_dispatch["acquisition"].arn
      },
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.outbox_relay.arn }
        Action    = "sqs:SendMessage"
        Resource  = [for queue in aws_sqs_queue.outbox : queue.arn]
      },
      {
        Effect    = "Allow"
        Principal = { AWS = [aws_iam_role.completion_consumer.arn, aws_iam_role.article_fetch.arn] }
        Action    = "sqs:ChangeMessageVisibility"
        Resource  = aws_sqs_queue.outbox["completion"].arn
      },
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.backfill.arn }
        Action    = "sqs:SendMessage"
        Resource  = [for stage in keys(local.backfill_stages) : aws_sqs_queue.outbox[stage].arn]
      },
    ])
  })
  tags = { Name = "${var.name_prefix}-vpce-sqs" }
}

resource "aws_sqs_queue_policy" "outbox" {
  for_each  = aws_sqs_queue.outbox
  queue_url = each.value.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = each.value.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenySendOutsideRelayEndpoint"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:SendMessage"
        Resource  = each.value.arn
        Condition = {
          StringNotEquals         = { "aws:sourceVpce" = aws_vpc_endpoint.outbox_sqs.id }
          StringNotEqualsIfExists = { "aws:CalledViaLast" = "sqs.amazonaws.com" }
        }
      },
    ]
  })
}
