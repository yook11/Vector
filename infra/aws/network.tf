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

# NAT Gateway の置き場。インターネットにアドレスを持つ唯一の外向き装置で、
# ここに居るのは AWS マネージドの NAT だけ。自前の task は 1 つも入らない。
resource "aws_subnet" "public_nat" {
  vpc_id            = aws_vpc.main.id
  availability_zone = var.az_primary
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 2)

  tags = { Name = "${var.name_prefix}-public-nat" }
}

# egress proxy 専用の private subnet。app 段の 20-26 は locals.stages から
# 生成される範囲なので、手書きの index をそこに混ぜない。
resource "aws_subnet" "proxy" {
  vpc_id            = aws_vpc.main.id
  availability_zone = var.az_primary
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 30)

  tags = { Name = "${var.name_prefix}-proxy" }
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

resource "aws_route_table_association" "public_nat" {
  subnet_id      = aws_subnet.public_nat.id
  route_table_id = aws_route_table.public.id
}

# --- NAT Gateway ----------------------------------------------------------
#
# 経路装置としてだけ置く。**強制力の担い手は NAT の不在ではなく、rt-app に
# 0.0.0.0/0 が無いこと。** NAT を引けるのは rt-proxy だけなので、戻しても
# proxy を迂回する経路は生まれない (NAT Gateway はルートテーブル経由の転送しか
# 行わず、private IP を直接宛先に指定しても応答しない)。
#
# 対価は月 $45。買っているのは「自前コンポーネントは public IP を持たない」と
# いう例外条項の無い不変条件。SG や Squid の src ACL と違い、設定の退行では
# 戻らない。ACL 評価はリクエストのパース後なので、パーサ段の脆弱性は ACL では
# 守れない — アドレスが無ければパケットがそもそも届かない。
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.name_prefix}-eip-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public_nat.id
  tags          = { Name = "${var.name_prefix}-nat" }

  # EIP が IGW 経由で到達可能になってから作る。
  depends_on = [aws_internet_gateway.main]
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

# VPC の外へ出られる唯一の app 側ルートテーブル。所属するのは proxy subnet だけ。
# S3 の経路は aws_vpc_endpoint.s3 が注入する (image pull を app 段と同じ経路に
# 揃え、NAT のデータ処理課金も通らない)。
resource "aws_route_table" "proxy" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-rt-proxy" }
}

resource "aws_route" "proxy_default" {
  route_table_id         = aws_route_table.proxy.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main.id
}

resource "aws_route_table_association" "proxy" {
  subnet_id      = aws_subnet.proxy.id
  route_table_id = aws_route_table.proxy.id
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
