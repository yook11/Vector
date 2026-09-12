terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {
    region = "ap-northeast-1"
    # ローカル設定を渡し忘れた場合は実アカウントへの接続を拒否する。
    allowed_account_ids  = ["000000000000"]
    workspace_key_prefix = "smoke/_workspaces"
    encrypt              = true
    use_lockfile         = true
  }
}

provider "aws" {
  region              = local.region
  profile             = var.aws_profile
  allowed_account_ids = [var.expected_account_id]
  assume_role {
    role_arn = "arn:aws:iam::${var.expected_account_id}:role/vector-test/bootstrap/vector-test-terraform"
    duration = "1h"
  }
  default_tags { tags = local.tags }
}

locals {
  region    = "ap-northeast-1"
  prefix    = "vector-test-${var.run_id}"
  proxy_ip  = "10.80.0.10"
  proxy_url = "http://${local.proxy_ip}:3128"
  registry  = "${var.expected_account_id}.dkr.ecr.${local.region}.amazonaws.com"
  images = {
    backend = "${local.registry}/vector-test/backend@${var.backend_image_digest}"
    proxy   = "${local.registry}/vector-test/proxy@${var.proxy_image_digest}"
  }
  tags = {
    Project = "vector-test", ManagedBy = "terraform", Lifecycle = "smoke"
    RunId   = var.run_id, SourceRevision = var.source_revision
  }
}
