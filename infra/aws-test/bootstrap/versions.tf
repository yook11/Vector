terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  # state用バケット自身は、Git管理外のlocal stateで管理する。
}

provider "aws" {
  region              = "ap-northeast-1"
  profile             = var.aws_profile
  allowed_account_ids = [var.expected_account_id]
  default_tags {
    tags = { Project = "vector-test", ManagedBy = "terraform", Lifecycle = "bootstrap" }
  }
}

variable "expected_account_id" {
  type        = string
  description = "作業先として確認したテストアカウントID。"
  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "確認済みのテストアカウントIDを12桁で指定してください。"
  }
}

locals {
  region       = "ap-northeast-1"
  account_id   = var.expected_account_id
  state_bucket = "vector-test-tfstate-${local.account_id}"
  role_path    = "/vector-test/bootstrap/"
  runtime_path = "/vector-test/runtime/"
  parameter    = "/vector-test/embedding-consumer/gemini-api-key"
}

variable "aws_profile" {
  type        = string
  description = "ローカルで設定したテスト管理者のAWS CLIプロファイル名。"
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]*$", var.aws_profile))
    error_message = "AWS CLIプロファイル名を明示してください。"
  }
}

variable "trusted_admin_role_arn_pattern" {
  type        = string
  description = "構築ロールを引き受けるSSO権限セットのARN（末尾の割当suffixだけを*にする）。"
  validation {
    condition = can(regex(
      "^arn:aws:iam::${var.expected_account_id}:role/aws-reserved/sso[.]amazonaws[.]com/(ap-northeast-1/)?AWSReservedSSO_[A-Za-z0-9_+=,.@-]+_[*]$",
      var.trusted_admin_role_arn_pattern
    ))
    error_message = "同じアカウントのSSO権限セットを1つ指定してください。"
  }
}
