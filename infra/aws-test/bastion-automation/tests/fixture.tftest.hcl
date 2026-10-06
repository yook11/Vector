mock_provider "aws" {
  mock_resource "aws_vpc" { defaults = { id = "vpc-00000000000000001" } }
  mock_resource "aws_security_group" { defaults = { id = "sg-00000000000000001" } }
  mock_resource "aws_vpc_endpoint" { defaults = { id = "vpce-00000000000000001" } }

  override_during = plan
  mock_data "aws_caller_identity" { defaults = { account_id = "733360597472" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::733360597472:role/vector-test/bastion-automation/test" } }
  mock_resource "aws_sqs_queue" { defaults = { arn = "arn:aws:sqs:ap-northeast-1:733360597472:test", url = "https://sqs.ap-northeast-1.amazonaws.com/733360597472/test" } }
  mock_resource "aws_subnet" { defaults = { id = "subnet-00000000000000001" } }
}
override_module {
  target  = module.bastion
  outputs = { configuration = {} }
}
variables { run_id = "offline" }
run "test_account_and_private_boundary" {
  command = plan
  assert {
    condition = (
      local.account == "733360597472" &&
      local.region == "ap-northeast-1" &&
      aws_subnet.test["allowed"].vpc_id == aws_vpc.test.id &&
      toset(aws_network_interface.bastion.security_groups) == toset([aws_security_group.test["bastion"].id]) &&
      aws_iam_role.operations.max_session_duration == 3600 &&
      jsondecode(aws_iam_role.operations.assume_role_policy).Statement[0].Principal.AWS == aws_iam_role.investigator.arn
    )
    error_message = "別アカウントのprivate基盤とReadOnly相当→運用の入口を維持する。"
  }
  assert {
    condition = (
      jsondecode(aws_vpc_endpoint.test["sqs"].policy).Statement[0].Principal.AWS == aws_iam_role.operations.arn &&
      jsondecode(aws_vpc_endpoint.test["sqs"].policy).Statement[1].Resource == aws_sqs_queue.source.arn &&
      jsondecode(aws_sqs_queue_policy.source.policy).Statement[1].Condition.StringNotEqualsIfExists["aws:CalledViaLast"] == "sqs.amazonaws.com"
    )
    error_message = "本番同様に運用ロール・対象キュー・指定VPCEの境界を検証する。"
  }
}
