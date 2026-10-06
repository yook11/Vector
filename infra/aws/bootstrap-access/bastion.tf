variable "bastion_network" {
  description = "通常インフラのbastion_network出力を管理者が確認して渡す。"
  type        = object({ vpc_id = string, subnet_id = string, security_group_id = string, network_interface_id = string })
}
module "bastion" {
  source               = "../../modules/bastion-automation"
  name_prefix          = "vector"
  account_id           = local.account_id
  role_path            = "/vector-ops/"
  operations_role_name = aws_iam_role.operations.name
  network              = var.bastion_network
}
output "bastion_automation" { value = module.bastion.configuration }
