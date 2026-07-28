# NAT を置かない構成では、ECS が task を起動するために使う経路 (image pull /
# secret 注入 / log 送信) を別に確保しないと task が起動しない。これらは task の
# HTTPS_PROXY を経由せず ECS 側が行うため、proxy では代替できない。

# 無料。ECR のレイヤーは S3 から来るのでデータ転送費も減る。
# interface endpoint と違い、ルートテーブルに entry を持つのはこれだけ。
#
# endpoint policy が 4 層目の境界になる。SG は「S3 へ 443 で出られる」までしか
# 絞れず、そのままだと frontend を含む全段がリージョンの S3 全域に到達できる
# (public-writable bucket への PUT は credential 不要なので exfil 経路が復活し、
# task role が空でも防げない)。policy で ECR のレイヤー bucket への read だけに
# 縛ることで、frontend の外向き経路を image pull のみに戻す。
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.app.id, aws_route_table.proxy.id]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EcrImageLayersOnly"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = ["arn:aws:s3:::prod-${var.region}-starport-layer-bucket/*"]
      },
    ]
  })

  tags = { Name = "${var.name_prefix}-vpce-s3" }
}

# interface endpoint は 1 AZ につき 1 subnet にしか ENI を置けない。
# 7 つの app subnet 全部に置く必要はなく、api の subnet に集約して
# 他の subnet からは VPC の local ルートで届かせる (到達制御は SG が行う)。
# これで課金は「4 endpoint × 1 AZ」に収まる。
#
# ssm = Parameter Store からの secret 注入 (execution role が実行する)。
#
# ecs / ecs-agent / ecs-telemetry は置かない。この 3 つは EC2 launch type の
# ECS agent 用で、Fargate task には不要 (AWS docs 明記)。
# ECS Exec を使うと決めた場合は ssmmessages の endpoint がここに 1 本増える
# (task role の ssmmessages:* とセット)。課金根拠の「4 本」もそこで変わる。
resource "aws_vpc_endpoint" "interface" {
  for_each = toset(["ecr.api", "ecr.dkr", "ssm", "logs"])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app["api"].id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.name_prefix}-vpce-${replace(each.value, ".", "-")}" }
}
