variable "expected_account_id" {
  type        = string
  description = "管理者が確認したVector本番アカウントID。"
  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "対象アカウントIDを12桁で明示してください。"
  }
}

variable "identity_center_region" {
  type        = string
  description = "Identity Centerのリージョンであり、リソース配置先とは独立する。"
  default     = "ap-northeast-1"
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.identity_center_region))
    error_message = "Identity Centerのリージョン名を指定してください。"
  }
}

variable "hosted_zone_id" {
  type        = string
  description = "既存bootstrapが管理するpublic hosted zoneのID。"
  validation {
    condition     = can(regex("^Z[A-Z0-9]+$", var.hosted_zone_id))
    error_message = "既存hosted zoneのIDをZから始まる形式で指定してください。"
  }
}
