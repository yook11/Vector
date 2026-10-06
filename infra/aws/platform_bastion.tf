# 踏み台の接続基盤は常設し、EC2の作成・撤去は固定Automationだけが行う。

resource "aws_subnet" "bastion" {
  vpc_id            = aws_vpc.main.id
  availability_zone = var.az_primary
  cidr_block        = local.subnet_cidrs["bastion"]

  tags = { Name = "${var.name_prefix}-bastion" }
}

resource "aws_route_table_association" "bastion" {
  subnet_id      = aws_subnet.bastion.id
  route_table_id = aws_route_table.data.id
}

resource "aws_vpc_endpoint" "ssmmessages" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.ssmmessages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app["api"].id]
  security_group_ids  = [aws_security_group.ssmmessages_endpoint.id]
  private_dns_enabled = true

  tags = { Name = "${var.name_prefix}-vpce-ssmmessages" }
}

resource "aws_security_group" "ssmmessages_endpoint" {
  name        = "${var.name_prefix}-vpce-ssmmessages"
  description = "Session Manager data channel. Reachable only from the bastion."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "ssmmessages_from_bastion" {
  security_group_id            = aws_security_group.ssmmessages_endpoint.id
  description                  = "bastion"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.bastion.id
}

resource "aws_security_group" "bastion" {
  name        = "${var.name_prefix}-bastion"
  description = "Temporary DB bastion. No ingress; SSM connects outbound."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "bastion_to_endpoints" {
  security_group_id            = aws_security_group.bastion.id
  description                  = "ssm (agent registration)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.endpoints.id
}

resource "aws_vpc_security_group_egress_rule" "bastion_to_ssmmessages" {
  security_group_id            = aws_security_group.bastion.id
  description                  = "ssmmessages (session channel)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.ssmmessages_endpoint.id
}

resource "aws_vpc_security_group_egress_rule" "bastion_to_sqs" {
  security_group_id            = aws_security_group.bastion.id
  referenced_security_group_id = aws_security_group.outbox_sqs_endpoint.id
  description                  = "SQS operations tunnel"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "sqs_from_bastion" {
  security_group_id            = aws_security_group.outbox_sqs_endpoint.id
  referenced_security_group_id = aws_security_group.bastion.id
  description                  = "Temporary bastion"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "bastion_to_rds" {
  security_group_id            = aws_security_group.bastion.id
  description                  = "postgres"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_bastion" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "bastion"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.bastion.id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_bastion" {
  security_group_id            = aws_security_group.rds.id
  description                  = "bastion"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.bastion.id
}


resource "aws_network_interface" "bastion" {
  subnet_id       = aws_subnet.bastion.id
  security_groups = [aws_security_group.bastion.id]
  description     = "Fixed primary interface for the operations bastion"
  tags            = { Name = "${var.name_prefix}-bastion", "vector:managed-bastion" = var.name_prefix }
}
moved {
  from = aws_subnet.bastion[0]
  to   = aws_subnet.bastion
}

moved {
  from = aws_route_table_association.bastion[0]
  to   = aws_route_table_association.bastion
}

moved {
  from = aws_vpc_endpoint.ssmmessages[0]
  to   = aws_vpc_endpoint.ssmmessages
}

moved {
  from = aws_security_group.ssmmessages_endpoint[0]
  to   = aws_security_group.ssmmessages_endpoint
}

moved {
  from = aws_vpc_security_group_ingress_rule.ssmmessages_from_bastion[0]
  to   = aws_vpc_security_group_ingress_rule.ssmmessages_from_bastion
}

moved {
  from = aws_security_group.bastion[0]
  to   = aws_security_group.bastion
}

moved {
  from = aws_vpc_security_group_egress_rule.bastion_to_endpoints[0]
  to   = aws_vpc_security_group_egress_rule.bastion_to_endpoints
}

moved {
  from = aws_vpc_security_group_egress_rule.bastion_to_ssmmessages[0]
  to   = aws_vpc_security_group_egress_rule.bastion_to_ssmmessages
}

moved {
  from = aws_vpc_security_group_egress_rule.bastion_to_sqs[0]
  to   = aws_vpc_security_group_egress_rule.bastion_to_sqs
}

moved {
  from = aws_vpc_security_group_ingress_rule.sqs_from_bastion[0]
  to   = aws_vpc_security_group_ingress_rule.sqs_from_bastion
}

moved {
  from = aws_vpc_security_group_egress_rule.bastion_to_rds[0]
  to   = aws_vpc_security_group_egress_rule.bastion_to_rds
}

moved {
  from = aws_vpc_security_group_ingress_rule.endpoints_from_bastion[0]
  to   = aws_vpc_security_group_ingress_rule.endpoints_from_bastion
}

moved {
  from = aws_vpc_security_group_ingress_rule.rds_from_bastion[0]
  to   = aws_vpc_security_group_ingress_rule.rds_from_bastion
}

# 既存IAMはAWS上に残し、移行手順に従ってbootstrap-accessへimportする。
removed {
  from = aws_iam_role.bastion
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_instance_profile.bastion
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy_attachment.bastion_ssm
  lifecycle { destroy = false }
}
