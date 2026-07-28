# Valkey は 2 ノード。同居できない理由は eviction 方針が真逆で、
# maxmemory-policy が parameter group = ノード単位の設定だから。
#
#   broker      noeviction    キューを捨てさせない
#   rate-limit  volatile-ttl  noeviction だと満杯時 write 拒否 → proxy.ts が
#                             fail-open して rate limit が全体 bypass される

locals {
  # broker ノードに繋ぐ段ごとに 1 user。棚卸しの read/write 非対称を写す
  # (api / scheduler は積むだけ、agent は取り出すだけ)。
  # access string と password は CLI で設定する (下記コメント参照)。
  broker_user_ids = [for s in local.broker_stages : "${var.name_prefix}-${s}"]
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name_prefix}-cache"
  subnet_ids = [for s in aws_subnet.data : s.id]
}

resource "aws_elasticache_parameter_group" "broker" {
  name   = "${var.name_prefix}-broker"
  family = "valkey${split(".", var.valkey_version)[0]}"

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }
}

resource "aws_elasticache_parameter_group" "rate_limit" {
  name   = "${var.name_prefix}-rate-limit"
  family = "valkey${split(".", var.valkey_version)[0]}"

  parameter {
    name  = "maxmemory-policy"
    value = "volatile-ttl"
  }
}

# user は Terraform で管理しない。
# aws_elasticache_user の authentication_mode.passwords は state に平文で載るため、
# SSM parameter と同じ理由で外に出す。
#
#   aws elasticache create-user --engine VALKEY --user-id vector-api \
#     --user-name vector-api --passwords '<16-128 chars>' \
#     --access-string 'on ~pipeline:dispatch ~pipeline:acquisition ~agent \
#       +@connection +xadd (~taskiq:* +@string) \
#       (~agent:run:* +xadd +xrange +xread +exists)'
#
# user group の membership は「どの段がこのノードに来るか」という構造なので
# Terraform に残す。これで Terraform が secret の実体に触る場所はゼロになる。
resource "aws_elasticache_user_group" "broker" {
  engine        = "valkey"
  user_group_id = "${var.name_prefix}-broker"
  # user は先に CLI で作っておく必要がある。存在しない ID を並べると apply が失敗する
  # (fail-closed なので気づける)。
  user_ids = local.broker_user_ids
}

resource "aws_elasticache_user_group" "rate_limit" {
  engine        = "valkey"
  user_group_id = "${var.name_prefix}-rate-limit"
  user_ids      = ["${var.name_prefix}-frontend"]
}

# cluster mode 無効 / shard 1 / replica 0。
# 安いから外すのではなく、要らない分散と冗長化を外す。喪失は back-fill cron が
# 救済し、救済経路の無い週次 briefing と agent run は別途受け入れ済み。
resource "aws_elasticache_replication_group" "broker" {
  replication_group_id = "${var.name_prefix}-broker"
  description          = "taskiq broker streams, result backend, agent live streams"

  engine         = "valkey"
  engine_version = var.valkey_version
  node_type      = "cache.t4g.micro"

  num_cache_clusters         = 1
  automatic_failover_enabled = false

  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = [aws_security_group.valkey_broker.id]
  parameter_group_name = aws_elasticache_parameter_group.broker.name

  # RBAC は転送時暗号化が前提。
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  user_group_ids             = [aws_elasticache_user_group.broker.user_group_id]

  # キューの喪失を受け入れる決定と揃える。スナップショットを取らない。
  snapshot_retention_limit = 0
  maintenance_window       = "thu:20:00-thu:21:00"
  apply_immediately        = var.apply_immediately

  tags = { Name = "${var.name_prefix}-broker" }
}

resource "aws_elasticache_replication_group" "rate_limit" {
  replication_group_id = "${var.name_prefix}-rate-limit"
  description          = "frontend proxy.ts sliding window rate limit"

  engine         = "valkey"
  engine_version = var.valkey_version
  node_type      = "cache.t4g.micro"

  num_cache_clusters         = 1
  automatic_failover_enabled = false

  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = [aws_security_group.valkey_rl.id]
  parameter_group_name = aws_elasticache_parameter_group.rate_limit.name

  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  user_group_ids             = [aws_elasticache_user_group.rate_limit.user_group_id]

  snapshot_retention_limit = 0
  maintenance_window       = "thu:21:00-thu:22:00"
  apply_immediately        = var.apply_immediately

  tags = { Name = "${var.name_prefix}-rate-limit" }
}
