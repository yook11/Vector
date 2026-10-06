terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "name_prefix" { type = string }
variable "account_id" { type = string }
variable "role_path" { type = string }
variable "operations_role_name" { type = string }
variable "network" {
  type = object({ vpc_id = string, subnet_id = string, security_group_id = string, network_interface_id = string })
}
locals {
  region  = "ap-northeast-1"
  ec2_arn = "arn:aws:ec2:${local.region}:${var.account_id}"
  ssm_arn = "arn:aws:ssm:${local.region}:${var.account_id}"
  tags = {
    Name                     = "${var.name_prefix}-bastion"
    "vector:managed-bastion" = var.name_prefix
    "vector:session-purpose" = "sqs-redrive"
  }
}
