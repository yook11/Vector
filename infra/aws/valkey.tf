# Valkey は 2 ノード。同居できない理由は eviction 方針が真逆で、
# maxmemory-policy が parameter group = ノード単位の設定だから。
#
#   broker      noeviction    キューを捨てさせない
#   rate-limit  volatile-ttl  noeviction だと満杯時 write 拒否 → proxy.ts が
#                             fail-open して rate limit が全体 bypass される

# access string は棚卸しで確定した「実際に発行するコマンド全量」を写す。
# 意図する非対称: api / scheduler はキューを消費できない (+xadd のみ)、
# agent はキューに積めない (root に +xadd が無い)。
#
# 非自明な制約:
# - ~pattern は glob。~agent はキュー stream の完全一致で、live 配信の
#   agent:run:* には当たらない。両方の列挙が必須。
# - Lua 内の redis.call も呼び出し user の ACL で検査されるため、+eval だけでなく
#   script が呼ぶコマンドを同じ selector に列挙する。
# - scheduler の +xgroup|create は taskiq の startup() が XGROUP CREATE MKSTREAM を
#   発行するため。XADD だけでは起動できない。
# - autoclaim:taskiq:<stream> は taskiq の再配達 lock。SET NX で取得し、
#   解放は EVALSHA の Lua が GET + DEL を呼ぶ。
locals {
  # redis-py 8 / node-redis 6 は RESP3 既定で HELLO を発行する。CLIENT (SETINFO 含む)
  # は @connection が丸ごと含むため個別指定しない。全体追加済み command への
  # subcommand 追加は ElastiCache の CreateUser が InvalidParameterValue で拒否する。
  valkey_common_acl = "+@connection -@dangerous"

  broker_user_access = {
    api = join(" ", [
      "on ~pipeline:dispatch ~pipeline:acquisition ~agent +xadd",
      "+multi +exec",
      local.valkey_common_acl,
      "(~agent:run:* +xadd +xrange +xread +exists +expire +lrange)",
    ])
    scheduler = join(" ", [
      "on ~pipeline:dispatch ~pipeline:maintenance ~trend_discovery ~briefing ~agent",
      "+xadd +xgroup|create",
      local.valkey_common_acl,
    ])
    fetch = join(" ", [
      "on ~pipeline:dispatch ~pipeline:acquisition ~pipeline:completion",
      "+xadd +xgroup|create +xreadgroup +xack +xautoclaim",
      "+multi +exec +script|exists +script|load",
      local.valkey_common_acl,
      "(~pipeline:curation +xadd)",
      "(~autoclaim:taskiq:pipeline:dispatch ~autoclaim:taskiq:pipeline:acquisition ~autoclaim:taskiq:pipeline:completion +set +get +del +evalsha)",
      "(~taskiq:* +set)",
    ])
    analysis = join(" ", [
      "on ~pipeline:curation ~pipeline:assessment ~pipeline:embedding ~pipeline:maintenance",
      "+xadd +xgroup|create +xreadgroup +xack +xautoclaim",
      "+multi +exec +time +script|exists +script|load",
      local.valkey_common_acl,
      "(~autoclaim:taskiq:pipeline:curation ~autoclaim:taskiq:pipeline:assessment ~autoclaim:taskiq:pipeline:embedding ~autoclaim:taskiq:pipeline:maintenance +set +get +del +evalsha)",
      "(~taskiq:* +set)",
      "(~ratelimit:* +evalsha +zremrangebyscore +zcard +zadd +zrange +expire)",
      "(~backfill:budget:* +eval +get +incrby +expire)",
      # recovery hold (現状 production 呼び出しゼロ) を有効化するときは
      # +eval +get +del +expire を足す。
      "(~curation:hold ~assessment:hold ~embedding:hold +set +exists)",
      "(~pipeline:acquisition ~pipeline:completion ~pipeline:curation ~pipeline:assessment +xlen +xinfo|groups +xpending +xrange)",
    ])
    insights = join(" ", [
      "on ~trend_discovery ~briefing",
      "+xgroup|create +xreadgroup +xack +xautoclaim",
      "+multi +exec +script|exists +script|load",
      local.valkey_common_acl,
      "(~briefing +xadd)",
      "(~autoclaim:taskiq:trend_discovery ~autoclaim:taskiq:briefing +set +get +del +evalsha)",
      "(~taskiq:* +set)",
    ])
    agent = join(" ", [
      "on ~agent",
      "+xgroup|create +xreadgroup +xack +xautoclaim",
      "+multi +exec +script|exists +script|load",
      local.valkey_common_acl,
      "(~agent:run:* +xadd +expire +lpush +ltrim +del)",
      "(~autoclaim:taskiq:agent +set +get +del +evalsha)",
      "(~taskiq:* +set)",
    ])
  }

  frontend_user_access = join(" ", [
    "on ~rl:* +eval +zremrangebyscore +zcard +zadd +expire",
    local.valkey_common_acl,
  ])
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

# IAM 認証なので password が存在せず、user を Terraform 管理にしても state に
# 秘密が載らない。IAM が決めるのは「どの user として繋げるか」(iam.tf) だけで、
# 入った後に何ができるかは access_string が全部決める。
resource "aws_elasticache_user" "broker" {
  for_each = local.broker_user_access

  user_id = "${var.name_prefix}-${each.key}"
  # IAM 認証は user_id と user_name の一致が必須。
  user_name     = "${var.name_prefix}-${each.key}"
  engine        = "valkey"
  access_string = each.value

  authentication_mode {
    type = "iam"
  }
}

resource "aws_elasticache_user" "frontend" {
  user_id       = "${var.name_prefix}-frontend"
  user_name     = "${var.name_prefix}-frontend"
  engine        = "valkey"
  access_string = local.frontend_user_access

  authentication_mode {
    type = "iam"
  }
}

# user group の membership は「どの段がこのノードに来るか」という構造。
# VALKEY engine の user group に default user は要らない (REDIS engine と要件が違う)。
resource "aws_elasticache_user_group" "broker" {
  engine        = "valkey"
  user_group_id = "${var.name_prefix}-broker"
  # broker_stages に居るのに access string が無い段は、この参照が plan で落ちる
  # (fail-closed なので気づける)。
  user_ids = [for s in local.broker_stages : aws_elasticache_user.broker[s].user_id]
}

resource "aws_elasticache_user_group" "rate_limit" {
  engine        = "valkey"
  user_group_id = "${var.name_prefix}-rate-limit"
  user_ids      = [aws_elasticache_user.frontend.user_id]
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
  # 既定値だが、ecs.tf が env の URL を組むのに参照するため明示する。
  port = 6379

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
  port           = 6379

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
