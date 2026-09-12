variable "expected_account_id" {
  type        = string
  description = "作業先として確認したテストアカウントID。"
  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "確認済みのテストアカウントIDを12桁で指定してください。"
  }
}
variable "run_id" {
  type        = string
  description = "stateキーと一致させる試験ID（英小文字・数字・ハイフン、最大24文字）。"
  validation {
    condition     = length(var.run_id) <= 24 && can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.run_id))
    error_message = "run_idは英小文字・数字と区切りのハイフンで1〜24文字にし、末尾や連続のハイフンを避けてください。"
  }
}
variable "backend_image_digest" {
  type        = string
  description = "テストECRに格納済みのARM64 backendイメージdigest。"
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.backend_image_digest))
    error_message = "backendのsha256 digestが必須です。"
  }
}
variable "proxy_image_digest" {
  type        = string
  description = "テストECRに格納済みのARM64 Squidイメージdigest。"
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.proxy_image_digest))
    error_message = "proxyのsha256 digestが必須です。"
  }
}
variable "source_revision" {
  type        = string
  description = "backendイメージに対応するGit commit SHA。"
  validation {
    condition     = can(regex("^[a-f0-9]{40}$", var.source_revision))
    error_message = "40桁のcommit SHAを指定してください。"
  }
}
variable "gemini_parameter_path" {
  type        = string
  description = "Terraform外で登録するテスト専用Standard SecureStringのパス。"
  validation {
    condition     = var.gemini_parameter_path == "/vector-test/embedding-consumer/gemini-api-key"
    error_message = "常設の権限境界が許可するテスト専用パスを指定してください。"
  }
}

variable "aws_profile" {
  type        = string
  description = "ローカルで設定したテスト管理者のAWS CLIプロファイル名。"
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]*$", var.aws_profile))
    error_message = "AWS CLIプロファイル名を明示してください。"
  }
}
