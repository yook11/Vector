# security group が決めるのは「VPC の中で誰に届くか」だけ。
# 規則は inline block ではなく個別 resource で宣言する (差分がレビューで読める)。
# 規則を 1 つも書かなければ全拒否なので、egress も必要な相手だけ明示する。

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Public entrypoint. The only path from the internet."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "app" {
  for_each = local.stages

  name        = "${var.name_prefix}-app-${each.key}"
  description = "ECS task: ${each.key}"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds"
  description = "RDS PostgreSQL. IAM DB auth."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "valkey_broker" {
  name        = "${var.name_prefix}-valkey-broker"
  description = "Valkey for taskiq broker and agent live streams. maxmemory-policy noeviction."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "valkey_rl" {
  name        = "${var.name_prefix}-valkey-rl"
  description = "Valkey for frontend proxy.ts rate limit. maxmemory-policy volatile-ttl."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "proxy" {
  name        = "${var.name_prefix}-proxy"
  description = "Egress proxy. Replaces the NAT Gateway."
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name_prefix}-vpce"
  description = "Interface VPC endpoints (ECR / SSM / CloudWatch Logs)."
  vpc_id      = aws_vpc.main.id
}

# --- 入口 -----------------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "alb_from_internet" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_frontend" {
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB to frontend task"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.app["frontend"].id
}

resource "aws_vpc_security_group_ingress_rule" "frontend_from_alb" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "ALB"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.alb.id
}

# backend は frontend 経由でしか到達できない (transport 層の defense in depth)。
# application 層の BFF_JWT_SIGNING_SECRET 検証と合わせて 2 層。
resource "aws_vpc_security_group_ingress_rule" "api_from_frontend" {
  security_group_id            = aws_security_group.app["api"].id
  description                  = "frontend BFF"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.app["frontend"].id
}

resource "aws_vpc_security_group_egress_rule" "frontend_to_api" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "backend API"
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.app["api"].id
}

# worker-insights は frontend に到達する唯一の backend (ISR revalidate 通知)。
# ALB からの経路と同じ port に来るので、区別は application 層の
# REVALIDATE_BEARER_SECRET が担う。
#
# 注意: worker-insights は HTTPS_PROXY を持ち、revalidate の送信は
# trust_env 既定 True の httpx なので env の proxy を拾う。NO_PROXY に
# 内部 frontend 名を入れないと、proxy の private 宛先拒否で静かに失敗する。
resource "aws_vpc_security_group_ingress_rule" "frontend_from_insights" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "worker-insights revalidate notification"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.app["insights"].id
}

resource "aws_vpc_security_group_egress_rule" "insights_to_frontend" {
  security_group_id            = aws_security_group.app["insights"].id
  description                  = "revalidate notification"
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
  referenced_security_group_id = aws_security_group.app["frontend"].id
}

# --- データストア ---------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "rds_from_app" {
  for_each = local.db_stages

  security_group_id            = aws_security_group.rds.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_rds" {
  for_each = local.db_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "RDS PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

# frontend は broker 側の Valkey に繋がない。rate-limit ノードは eviction 方針が
# 真逆 (volatile-ttl / noeviction) で、parameter group がノード単位なので別ノード。
resource "aws_vpc_security_group_ingress_rule" "valkey_broker_from_app" {
  for_each = local.broker_stages

  security_group_id            = aws_security_group.valkey_broker.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_valkey_broker" {
  for_each = local.broker_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "Valkey broker"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.valkey_broker.id
}

resource "aws_vpc_security_group_ingress_rule" "valkey_rl_from_frontend" {
  security_group_id            = aws_security_group.valkey_rl.id
  description                  = "frontend proxy.ts rate limit"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app["frontend"].id
}

resource "aws_vpc_security_group_egress_rule" "frontend_to_valkey_rl" {
  security_group_id            = aws_security_group.app["frontend"].id
  description                  = "Valkey rate limit"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.valkey_rl.id
}

# --- egress proxy ---------------------------------------------------------
#
# frontend は含めない。Logfire も外部 API も持たないため、外に出る手段を
# 1 つも与えない。入口を持つ唯一の段が egress ゼロになる。

resource "aws_vpc_security_group_ingress_rule" "proxy_from_app" {
  for_each = local.egress_stages

  security_group_id            = aws_security_group.proxy.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_proxy" {
  for_each = local.egress_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "egress proxy"
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
  referenced_security_group_id = aws_security_group.proxy.id
}

# どの宛先を許すかは proxy の allowlist が段ごとに決める。SG では port だけ。
resource "aws_vpc_security_group_egress_rule" "proxy_to_internet" {
  for_each = toset(["80", "443"])

  security_group_id = aws_security_group.proxy.id
  description       = "allowlisted upstreams"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
  cidr_ipv4         = "0.0.0.0/0"
}

# --- VPC endpoint ---------------------------------------------------------
#
# ENI は api の subnet 1 つだけに置くが、到達は VPC の local ルートで全 subnet
# から届く。ここを開けないと image pull も secret 注入も log 送信も落ちる。

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_app" {
  for_each = local.all_stages

  security_group_id            = aws_security_group.endpoints.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.app[each.value].id
}

# proxy も image pull と log 送信で endpoint を使う。private DNS は VPC 全域に
# 効くので、proxy をどの subnet に置いても ecr.api / logs は endpoint の ENI に
# 解決される。ここを開けないと proxy task 自体が起動しない。
# (S3 のレイヤー実体は gateway endpoint 経由なのでこの規則の対象外。)
resource "aws_vpc_security_group_ingress_rule" "endpoints_from_proxy" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "proxy"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.proxy.id
}

resource "aws_vpc_security_group_egress_rule" "app_to_endpoints" {
  for_each = local.all_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "ECR / SSM / CloudWatch Logs endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.endpoints.id
}

# ECR のレイヤー実体は S3 にあり、Fargate は task ENI から S3 へ直接取りに行く。
# ルートテーブルに Gateway endpoint の経路があっても、SG の egress が無ければ
# ここで落ちて task が起動しない (「規則ゼロは全拒否」がそのまま効く)。
# 宛先は gateway endpoint が export する prefix list で参照する。
#
# frontend にもこの穴が要る = 「frontend は外に出られない」が形式上は崩れる。
# 何を取れるかは aws_vpc_endpoint.s3 の endpoint policy が縛る (endpoints.tf)。
resource "aws_vpc_security_group_egress_rule" "app_to_s3" {
  for_each = local.all_stages

  security_group_id = aws_security_group.app[each.value].id
  description       = "ECR image layers via S3 Gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
}
