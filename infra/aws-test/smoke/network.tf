resource "aws_vpc" "smoke" {
  cidr_block           = "10.80.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = local.prefix })
  lifecycle {
    precondition {
      condition     = terraform.workspace == "default"
      error_message = "workspaceはdefault固定です。実行ごとの分離にはS3のstateキーを使用してください。"
    }
  }
}
locals {
  subnets = {
    proxy  = { cidr = "10.80.0.0/24", az = "ap-northeast-1a" }
    lambda = { cidr = "10.80.1.0/24", az = "ap-northeast-1a" }
    runner = { cidr = "10.80.2.0/24", az = "ap-northeast-1a" }
    db_a   = { cidr = "10.80.3.0/24", az = "ap-northeast-1a" }
    db_c   = { cidr = "10.80.4.0/24", az = "ap-northeast-1c" }
  }
}
resource "aws_subnet" "smoke" {
  for_each                = local.subnets
  vpc_id                  = aws_vpc.smoke.id
  cidr_block              = each.value.cidr
  availability_zone       = each.value.az
  map_public_ip_on_launch = false
  tags                    = merge(local.tags, { Name = "${local.prefix}-${each.key}" })
}
resource "aws_internet_gateway" "proxy" {
  vpc_id = aws_vpc.smoke.id
  tags   = merge(local.tags, { Name = "${local.prefix}-proxy" })
}
resource "aws_route_table" "smoke" {
  for_each = toset(["public", "private"])
  vpc_id   = aws_vpc.smoke.id
  tags     = merge(local.tags, { Name = "${local.prefix}-${each.key}" })
}
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.smoke["public"].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.proxy.id
}
resource "aws_route_table_association" "smoke" {
  for_each       = local.subnets
  subnet_id      = aws_subnet.smoke[each.key].id
  route_table_id = aws_route_table.smoke[each.key == "proxy" ? "public" : "private"].id
}
resource "aws_security_group" "smoke" {
  for_each    = toset(["lambda", "runner", "proxy", "db", "ssm"])
  name        = "${local.prefix}-${each.key}"
  description = "${each.key} traffic for isolated embedding smoke"
  vpc_id      = aws_vpc.smoke.id
  tags        = merge(local.tags, { Name = "${local.prefix}-${each.key}" })
}
locals {
  private_connections = {
    lambda_db    = { from = "lambda", to = "db", port = 5432 }
    runner_db    = { from = "runner", to = "db", port = 5432 }
    lambda_proxy = { from = "lambda", to = "proxy", port = 3128 }
    runner_proxy = { from = "runner", to = "proxy", port = 3128 }
    lambda_ssm   = { from = "lambda", to = "ssm", port = 443 }
    runner_ssm   = { from = "runner", to = "ssm", port = 443 }
    proxy_ssm    = { from = "proxy", to = "ssm", port = 443 }
  }
}
resource "aws_vpc_security_group_egress_rule" "private" {
  for_each                     = local.private_connections
  security_group_id            = aws_security_group.smoke[each.value.from].id
  referenced_security_group_id = aws_security_group.smoke[each.value.to].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  tags                         = merge(local.tags, { Name = "${local.prefix}-${each.key}" })
}
resource "aws_vpc_security_group_ingress_rule" "private" {
  for_each                     = local.private_connections
  security_group_id            = aws_security_group.smoke[each.value.to].id
  referenced_security_group_id = aws_security_group.smoke[each.value.from].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  tags                         = merge(local.tags, { Name = "${local.prefix}-${each.key}" })
}
resource "aws_vpc_security_group_egress_rule" "proxy_internet" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.smoke["proxy"].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
  tags              = merge(local.tags, { Name = "${local.prefix}-proxy-${each.key}" })
}
resource "aws_vpc_endpoint" "ssm" {
  vpc_id              = aws_vpc.smoke.id
  service_name        = "com.amazonaws.${local.region}.ssm"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [aws_subnet.smoke["lambda"].id]
  security_group_ids  = [aws_security_group.smoke["ssm"].id]
  tags                = merge(local.tags, { Name = "${local.prefix}-ssm" })
}
