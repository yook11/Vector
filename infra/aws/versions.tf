terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # state は S3 + ネイティブロック (DynamoDB を使わない)。
  # bucket は bootstrap スタックが作る (名前は account ID から導出)。
  # bucket 名は秘密ではないが公開 repo に置かないため、-backend-config で渡す:
  #   terraform init -backend-config="bucket=$(cd bootstrap && terraform output -raw state_bucket_name)"
  #
  # CI の plan は `-lock=false` で走らせる。native locking は state bucket に
  # lock オブジェクトを書くため、read-only の plan ロールでは失敗する。
  backend "s3" {
    key          = "main/terraform.tfstate"
    region       = "ap-northeast-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "vector"
      ManagedBy = "terraform"
    }
  }
}
