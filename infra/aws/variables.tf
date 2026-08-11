variable "region" {
  description = "全リソースを置くリージョン。"
  type        = string
  default     = "ap-northeast-1"
}

# 冗長化の水準は RDS Single-AZ が決める。ECS task も Valkey も同じ AZ に置き、
# cross-AZ 転送費をゼロにする。secondary は ALB と RDS subnet group が
# 仕様上 2 AZ を要求するためだけに存在する。
variable "az_primary" {
  description = "全 task / RDS / Valkey を置く AZ。"
  type        = string
  default     = "ap-northeast-1a"
}

variable "az_secondary" {
  description = "ALB と RDS subnet group の 2 AZ 要件を満たすためだけの AZ。"
  type        = string
  default     = "ap-northeast-1c"
}

variable "vpc_cidr" {
  description = "VPC の CIDR。subnet は /24 で切り出す。"
  type        = string
  default     = "10.0.0.0/16"
}

variable "name_prefix" {
  description = "リソース名の接頭辞。"
  type        = string
  default     = "vector"
}

variable "permissions_boundary_arn" {
  description = <<-EOT
    このスタックが作る全ロールに付ける permissions boundary。
    bootstrap スタックの出力を渡す。data source ではなく variable で受けるのは、
    apply ロールに /vector-ci/ への iam:Get* を要求しないため。
  EOT
  type        = string
}

variable "postgres_major_version" {
  description = <<-EOT
    メジャーバージョンのみ指定し、minor は auto_minor_version_upgrade に任せる
    (固定すると自動更新のたびに drift する)。
    halfvec を使うため pgvector 0.8.0 が要り、PostgreSQL 17.1 以降が条件。
    db.t4g.micro で提供されるのは 17.9 / 17.10 (2026-07-28 実測) なので満たす。
  EOT
  type        = string
  default     = "17"
}

variable "db_backup_retention_days" {
  description = "自動バックアップの保持日数。DB サイズまでのバックアップ保存は無課金。"
  type        = number
  default     = 7
}

variable "db_deletion_protection" {
  description = <<-EOT
    検証後に destroy する前提で false。定常運用に移すなら true にする。
    skip_final_snapshot はこの値の反転に連動させてある。
  EOT
  type        = bool
  default     = false
}

variable "root_domain" {
  description = <<-EOT
    Route 53 の public hosted zone 名 (末尾ドット無し)。
    apply の前に登録済みであること。data source で参照するので、
    無ければここで明確に失敗する。
  EOT
  type        = string
}

variable "frontend_domain" {
  description = "ALB に向ける FQDN。ACM 証明書と Route 53 の A レコードに使う。"
  type        = string
}

variable "enable_db_bastion" {
  description = <<-EOT
    DB への人手作業 (移行・保守) 用の一時踏み台を生やす (bastion.tf)。
    既定 false = 存在しない。素の apply が撤去を兼ねるため、踏み台を
    使う作業の最中に apply するときは必ず -var enable_db_bastion=true を付ける。
  EOT
  type        = bool
  default     = false
}

variable "crossref_contact_email" {
  description = <<-EOT
    Crossref API の User-Agent に載せる連絡先 (backend Settings の必須項目)。
    実際に使うのは fetch だけだが、Settings が構築時に全段で要求する。
    公開 repo に実アドレスを置かないため default を持たず tfvars で渡す。
  EOT
  type        = string
}

variable "internal_namespace" {
  description = <<-EOT
    Cloud Map の private DNS namespace。
    backend / frontend の起動時ガードがこの値を接尾辞として許す
    (`.flycast` との union。ガード自体は外さない)。
    app 側と共有する契約なので、変えるなら次の定数も同時に変える。
      backend  app/config.py       _ALLOWED_INTERNAL_HOST_SUFFIXES
      frontend lib/api/internal-config.ts _ALLOWED_INTERNAL_API_HOST_SUFFIXES
  EOT
  type        = string
  default     = "vector.internal"
}

variable "image_tag" {
  description = <<-EOT
    ECR の image tag。repository が IMMUTABLE なので commit SHA など一意な値を使う。
    実際の更新は rollout job が行い、Terraform は task_definition を
    ignore_changes で追わない。
  EOT
  type        = string
  default     = "bootstrap"
}

variable "enable_http_redirect" {
  description = "ALB に 80 → 443 の redirect listener を置くか。SG の 80 ingress も連動する。"
  type        = bool
  default     = true
}

variable "proxy_image_tag" {
  description = <<-EOT
    egress proxy の image tag。app の commit SHA とは無関係に動くので
    var.image_tag と共有しない (squid のバージョン基準で振る)。
    ignore_changes を付けていないため、apply のたびにこの値へ揃う。
  EOT
  type        = string
  default     = "6.12-1"
}

variable "proxy_port" {
  description = "egress proxy の listen port。security group の proxy 規則と揃える。"
  type        = number
  default     = 3128
}

variable "apply_immediately" {
  description = <<-EOT
    RDS / ElastiCache の変更を即時適用する。false だと次の maintenance window
    (JST 木 05:00 / 金 05:00) まで反映されず、検証中のイテレーションが噛み合わない。
    定常運用に移すときは false にする。
  EOT
  type        = bool
  default     = true
}

variable "valkey_version" {
  description = <<-EOT
    ACL selector は Redis 7.0 で導入され、7.2.14 と 8.1.9 の両方で
    同一挙動を実測済み (2026-07-27 / 07-28)。バージョン選択は設計に影響しない。
  EOT
  type        = string
  default     = "8.0"
}

variable "log_retention_days" {
  description = "CloudWatch Logs の保持期間。設定しないと無期限で課金が増え続ける。"
  type        = number
  default     = 30
}

variable "ecr_retained_images" {
  description = "ECR に残す image 数。超えた分は lifecycle policy で失効させる。"
  type        = number
  default     = 10
}

variable "slack_team_id" {
  description = <<-EOT
    アラート通知先 Slack workspace の ID (例 T0123ABC456)。
    コンソールの AWS Chatbot > Configured clients > Workspace details から取得する。
    公開 repo に実値を置かないため default を持たず、tfvars / CI secrets で渡す。
  EOT
  type        = string
}

variable "slack_channel_id" {
  description = <<-EOT
    アラート通知先 Slack channel の ID (例 C0123ABC456)。
    Slack の channel 詳細画面の最下部から取得する。
    公開 repo に実値を置かないため default を持たず、tfvars / CI secrets で渡す。
  EOT
  type        = string
}
