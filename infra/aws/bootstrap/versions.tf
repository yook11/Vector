terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # backend を宣言しない。このスタックは state bucket 自身を作るので、
  # local state のままにする (鶏卵の断ち切り方)。
  # 手元の terraform.tfstate は .gitignore で除外済み。
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "vector"
      ManagedBy = "terraform-bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}
