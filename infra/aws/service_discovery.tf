# 内部到達は Cloud Map の service discovery。
# ECS Service Connect は Envoy sidecar が各 task に載ってサイズ表を壊すので採らない。
# 到達制御は security group だけが行い、名前解決は権限に関与しない。
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = var.internal_namespace
  description = "Internal service discovery for Vector"
  vpc         = aws_vpc.main.id
}

# ここで登録される名前を起動時ガードが接尾辞で受理する。
#   backend  config.py の _enforce_internal_namespace_in_production
#   frontend lib/api/internal-config.ts
# どちらも意図的な構造的契約なので、外さずに `.flycast` との union で許す。
resource "aws_service_discovery_service" "this" {
  for_each = local.discoverable_stages

  name = each.value

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  # ECS が task の生死に応じて登録・解除する。Cloud Map 自身の health check は
  # 使わない (課金対象で、ECS の管理と二重になる)。
  # failure_threshold は AWS 側で 1 固定だが、空ブロックだと provider が API に
  # 送らず実体が null になり、毎 plan が replace を要求し続ける。固定値を明示して
  # round-trip を成立させる。
  health_check_custom_config {
    failure_threshold = 1
  }
}
