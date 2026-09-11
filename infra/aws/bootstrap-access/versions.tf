terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  # 実行権限の管理を既存bootstrapのstateから分離する。
}

provider "aws" {
  region              = "ap-northeast-1"
  profile             = "vector-admin"
  allowed_account_ids = [var.expected_account_id]
  default_tags {
    tags = { Project = "vector", ManagedBy = "terraform-bootstrap-access" }
  }
}

data "aws_caller_identity" "current" {}
