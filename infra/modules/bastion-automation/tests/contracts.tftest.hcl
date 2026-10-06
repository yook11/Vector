mock_provider "aws" {
  override_during = plan
  mock_data "aws_ssm_parameter" { defaults = { value = "ami-00000000000000001" } }
  mock_data "aws_ami" { defaults = { id = "ami-00000000000000001", architecture = "arm64", root_device_name = "/dev/xvda" } }
  mock_data "aws_network_interface" { defaults = { vpc_id = "vpc-00000000000000001", subnet_id = "subnet-00000000000000001", security_groups = ["sg-00000000000000001"] } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/vector-ops/test" } }
  mock_resource "aws_iam_instance_profile" { defaults = { arn = "arn:aws:iam::123456789012:instance-profile/test" } }
  mock_resource "aws_launch_template" { defaults = { id = "lt-00000000000000001", arn = "arn:aws:ec2:ap-northeast-1:123456789012:launch-template/lt-00000000000000001", latest_version = 3 } }
  mock_resource "aws_ssm_document" { defaults = { arn = "arn:aws:ssm:ap-northeast-1:123456789012:automation-definition/test", latest_version = "2" } }
}
variables {
  name_prefix          = "vector"
  account_id           = "123456789012"
  role_path            = "/vector-ops/"
  operations_role_name = "vector-operations"
  network              = { vpc_id = "vpc-00000000000000001", subnet_id = "subnet-00000000000000001", security_group_id = "sg-00000000000000001", network_interface_id = "eni-00000000000000001" }
}
run "fixed_launch" {
  command = plan
  assert {
    condition = (
      aws_launch_template.bastion.instance_type == "t4g.nano" &&
      aws_launch_template.bastion.user_data == null &&
      one(aws_launch_template.bastion.network_interfaces).network_interface_id == var.network.network_interface_id &&
      one(aws_launch_template.bastion.network_interfaces).device_index == 0 &&
      one(aws_launch_template.bastion.network_interfaces).delete_on_termination == "false" &&
      one(aws_launch_template.bastion.network_interfaces).subnet_id == null &&
      one(aws_launch_template.bastion.metadata_options).http_tokens == "required" &&
      one(one(aws_launch_template.bastion.block_device_mappings).ebs).encrypted == "true" &&
      one(one(aws_launch_template.bastion.block_device_mappings).ebs).delete_on_termination == "true" &&
      one(one(aws_launch_template.bastion.block_device_mappings).ebs).volume_size == 8 &&
      aws_iam_role_policy_attachment.ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    )
    error_message = "固定ENIを保持し、8GiB暗号化rootだけを削除するSSM専用起動設定を維持する。"
  }
}
run "fixed_automation" {
  command = plan
  assert {
    condition = (
      length(jsondecode(aws_ssm_document.create.content).parameters) == 0 &&
      keys(jsondecode(aws_ssm_document.destroy.content).parameters) == ["InstanceId"] &&
      jsondecode(aws_ssm_document.create.content).assumeRole == aws_iam_role.automation.arn &&
      jsondecode(aws_ssm_document.create.content).mainSteps[0].inputs.InputPayload.config.template_version == "3" &&
      jsondecode(aws_ssm_document.create.content).mainSteps[1].timeoutSeconds == 600 &&
      jsondecode(aws_ssm_document.create.content).mainSteps[0].onFailure == "step:cleanup"
    )
    error_message = "起動設定と実行ロールは固定し、失敗時は実行IDから所有者を再照合する。"
  }
  assert {
    condition = alltrue([for statement in slice(jsondecode(aws_iam_role_policy.operator.policy).Statement, 0, 2) : statement.Condition["ForAnyValue:StringEquals"]["ssm:DocumentVersion"] == ["2"] && startswith(statement.Resource, "arn:aws:ssm:ap-northeast-1:123456789012:document/vector-bastion-")]) && alltrue([
      for statement in jsondecode(aws_iam_role_policy.operator.policy).Statement :
      !contains(flatten([statement.Action]), "ec2:RunInstances") && !contains(flatten([statement.Action]), "ssm:SendAutomationSignal") && !contains(flatten([statement.Action]), "sts:AssumeRole")
    ])
    error_message = "操作者は承認済み数値版だけを開始し、直接EC2操作やステップ再開は行えない。"
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.automation.assume_role_policy).Statement[0].Principal.Service == "ssm.amazonaws.com" &&
      jsondecode(aws_iam_role.automation.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == var.account_id &&
      jsondecode(aws_iam_role.automation.assume_role_policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:ssm:ap-northeast-1:123456789012:automation-execution/*" &&
      contains(jsondecode(aws_iam_role_policy.automation.policy).Statement[0].Resource, "arn:aws:ec2:ap-northeast-1:123456789012:network-interface/eni-00000000000000001") &&
      !can(jsondecode(aws_iam_role_policy.automation.policy).Statement[0].Condition.StringEquals)
    )
    error_message = "実行ロールは同一アカウント・東京のSSMだけを信頼し、既存ENIに新規作成タグ条件を要求しない。"
  }
}
