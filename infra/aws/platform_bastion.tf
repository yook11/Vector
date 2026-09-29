# DB への人手作業 (移行・保守) のための一時踏み台。平常時は存在しない
# (enable_db_bastion = false が既定で、素の apply が撤去を兼ねる)。
#
# RDS は public IP もインターネット経路も持たないため、psql / alembic /
# pg_restore を届ける手段がこの toggle 以外に存在しない。経路は
# SSM Session Manager の port forwarding で、踏み台は public IP も
# SSH ポートも持たず、ingress 規則ゼロ (SSM は踏み台側からの外向き接続)。
#
# RDS SG / endpoints SG へ開ける穴もこの file の conditional resource なので、
# toggle を戻せば穴ごと消える。使い方と手順上の罠は README の「DB 踏み台」節。

# 専用 subnet を rt-data (local 経路のみ) に紐づける。app subnet に間借りすると
# 「subnet = egress proxy の権限単位」の身元が混ざるため分ける。
resource "aws_subnet" "bastion" {
  count = var.enable_db_bastion ? 1 : 0

  vpc_id            = aws_vpc.main.id
  availability_zone = var.az_primary
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 31)

  tags = { Name = "${var.name_prefix}-bastion" }
}

resource "aws_route_table_association" "bastion" {
  count = var.enable_db_bastion ? 1 : 0

  subnet_id      = aws_subnet.bastion[0].id
  route_table_id = aws_route_table.data.id
}

# Session Manager のデータチャネル。常設の 4 本 (endpoints.tf) に無いのは
# ECS Exec を使わない決定のためで、踏み台が居る間だけここで足す。
# agent の登録経路は常設の ssm endpoint が担う。
#
# SG は共有の endpoints SG ではなく専用 SG。共有 SG には app 7 段からの 443 を
# 受け入れる規則が既にあり、相乗りすると toggle on の間だけ app 段 →
# ssmmessages の経路が生まれる (実害は IAM で塞がっているが、この repo の思想は
# 「経路が存在しない」)。app 段の egress は共有 SG 宛の SG 参照なので、
# 専用 SG の ENI には最初から届かず、app 側の規則を触らずに両方向が閉じる。
resource "aws_vpc_endpoint" "ssmmessages" {
  count = var.enable_db_bastion ? 1 : 0

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.ssmmessages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app["api"].id]
  security_group_ids  = [aws_security_group.ssmmessages_endpoint[0].id]
  private_dns_enabled = true

  tags = { Name = "${var.name_prefix}-vpce-ssmmessages" }
}

resource "aws_security_group" "ssmmessages_endpoint" {
  count = var.enable_db_bastion ? 1 : 0

  name        = "${var.name_prefix}-vpce-ssmmessages"
  description = "Session Manager data channel. Reachable only from the bastion."
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "ssmmessages_from_bastion" {
  count = var.enable_db_bastion ? 1 : 0

  security_group_id            = aws_security_group.ssmmessages_endpoint[0].id
  description                  = "bastion"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.bastion[0].id
}

resource "aws_security_group" "bastion" {
  count = var.enable_db_bastion ? 1 : 0

  name        = "${var.name_prefix}-bastion"
  description = "Temporary DB bastion. No ingress; SSM connects outbound."
  vpc_id      = aws_vpc.main.id
}

# agent 登録 (常設の ssm endpoint = 共有 SG) とセッション本体 (専用 SG) の 2 宛先。
resource "aws_vpc_security_group_egress_rule" "bastion_to_endpoints" {
  count = var.enable_db_bastion ? 1 : 0

  security_group_id            = aws_security_group.bastion[0].id
  description                  = "ssm (agent registration)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.endpoints.id
}

resource "aws_vpc_security_group_egress_rule" "bastion_to_ssmmessages" {
  count = var.enable_db_bastion ? 1 : 0

  security_group_id            = aws_security_group.bastion[0].id
  description                  = "ssmmessages (session channel)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.ssmmessages_endpoint[0].id
}

resource "aws_vpc_security_group_egress_rule" "bastion_to_rds" {
  count = var.enable_db_bastion ? 1 : 0

  security_group_id            = aws_security_group.bastion[0].id
  description                  = "postgres"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

# 受け入れ側の扉 2 枚。どちらも toggle の中なので、撤去時に穴ごと消える。
resource "aws_vpc_security_group_ingress_rule" "endpoints_from_bastion" {
  count = var.enable_db_bastion ? 1 : 0

  security_group_id            = aws_security_group.endpoints.id
  description                  = "bastion"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.bastion[0].id
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_bastion" {
  count = var.enable_db_bastion ? 1 : 0

  security_group_id            = aws_security_group.rds.id
  description                  = "bastion"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.bastion[0].id
}

# permissions boundary は付けない。boundary は ssmmessages:* を Deny しており
# (NoEcsExec)、付けると Session Manager のデータチャネルが死ぬ。踏み台は
# 手元 admin の手動 apply 専用で、CI が boundary 無しの role を作ろうとすれば
# DenyRoleCreationWithoutBoundary で明示的に失敗する (fail-closed)。
resource "aws_iam_role" "bastion" {
  count = var.enable_db_bastion ? 1 : 0

  name = "${var.name_prefix}-bastion"
  path = "/${var.name_prefix}-ops/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "ec2.amazonaws.com" }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  count = var.enable_db_bastion ? 1 : 0

  role       = aws_iam_role.bastion[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  count = var.enable_db_bastion ? 1 : 0

  name = "${var.name_prefix}-bastion"
  role = aws_iam_role.bastion[0].name
}

data "aws_ssm_parameter" "bastion_ami" {
  count = var.enable_db_bastion ? 1 : 0

  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "bastion" {
  count = var.enable_db_bastion ? 1 : 0

  ami                    = data.aws_ssm_parameter.bastion_ami[0].value
  instance_type          = "t4g.nano"
  subnet_id              = aws_subnet.bastion[0].id
  vpc_security_group_ids = [aws_security_group.bastion[0].id]
  iam_instance_profile   = aws_iam_instance_profile.bastion[0].name

  associate_public_ip_address = false

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    encrypted = true
  }

  # 作業期間中に新 AL2023 AMI が出ても踏み台を作り直さない (トンネルが切れ、
  # instance-id も変わる)。次に toggle off -> on した時点で最新へ追従する。
  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "${var.name_prefix}-bastion" }
}
