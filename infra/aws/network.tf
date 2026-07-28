resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  # interface endpoint の private DNS に必須。無効だと ECR / SSM / Logs の
  # 名前が endpoint の ENI ではなく public IP に解決され、経路が無いので task が
  # 起動しなくなる。
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-igw" }
}

# --- subnet ---------------------------------------------------------------
#
# subnet が権限の単位になる。egress proxy が識別できるのは送信元 IP だけで
# security group は見えないため、allowlist を分けたい粒度で subnet を分ける。
# subnet 自体に課金は無い。

# ALB は仕様上 2 AZ を要求する。target が 1 AZ でも動き、cross-zone 転送は無課金。
resource "aws_subnet" "public_alb" {
  for_each = {
    primary   = { az = var.az_primary, index = 0 }
    secondary = { az = var.az_secondary, index = 1 }
  }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.value.az
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value.index)

  tags = { Name = "${var.name_prefix}-public-alb-${each.key}" }
}

# NAT Gateway を置かない代わりの唯一の出口。proxy task はここに public IP 付きで
# 起動する。VPC の外に出られる app 経路はこの 1 本だけになる。
resource "aws_subnet" "public_egress" {
  vpc_id            = aws_vpc.main.id
  availability_zone = var.az_primary
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 2)

  tags = { Name = "${var.name_prefix}-public-egress" }
}

# RDS subnet group も 2 AZ を要求する。インスタンス自体は primary AZ に置く。
resource "aws_subnet" "data" {
  for_each = {
    primary   = { az = var.az_primary, index = 10 }
    secondary = { az = var.az_secondary, index = 11 }
  }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.value.az
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, each.value.index)

  tags = { Name = "${var.name_prefix}-data-${each.key}" }
}

resource "aws_subnet" "app" {
  for_each = local.stages

  vpc_id            = aws_vpc.main.id
  availability_zone = var.az_primary
  cidr_block        = local.app_subnet_cidrs[each.key]

  tags = { Name = "${var.name_prefix}-app-${each.key}" }
}

# --- route table ----------------------------------------------------------
#
# ルートテーブルが決めるのは「VPC の外に出られるか」の 1 点だけ。
# どの外部ホストに出られるかは proxy の allowlist、VPC 内で誰に届くかは
# security group が決める。3 つの層を混ぜない。

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-rt-public" }
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public_alb" {
  for_each = aws_subnet.public_alb

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_egress" {
  subnet_id      = aws_subnet.public_egress.id
  route_table_id = aws_route_table.public.id
}

# app 段の構造的な保証: このルートテーブルに 0.0.0.0/0 を置かない。
# 設定でうっかり外に出るのではなく、経路が存在しない。
# S3 Gateway endpoint のルートは aws_vpc_endpoint.s3 が注入する
# (ECR のレイヤーが S3 から来るため、image pull に必要)。
resource "aws_route_table" "app" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-rt-app" }
}

resource "aws_route_table_association" "app" {
  for_each = aws_subnet.app

  subnet_id      = each.value.id
  route_table_id = aws_route_table.app.id
}

# RDS / Valkey は image pull もログ送信もしないため S3 への経路すら要らない。
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-rt-data" }
}

resource "aws_route_table_association" "data" {
  for_each = aws_subnet.data

  subnet_id      = each.value.id
  route_table_id = aws_route_table.data.id
}
