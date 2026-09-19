mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_kms_key" { defaults = { arn = "arn:aws:kms:ap-northeast-1:123456789012:key/test" } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_s3_bucket" { defaults = { arn = "arn:aws:s3:::test" } }
  mock_resource "aws_iam_openid_connect_provider" { defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" } }
}

variables {
  name_prefix  = "slice-test"
  github_owner = "test-owner"
  github_repo  = "test-repo"
  root_domain  = "example.com"
}

run "rollout_updates_only_prefixed_lambda_images_from_backend_repository" {
  command = plan

  assert {
    condition = [
      for statement in jsondecode(aws_iam_role_policy.rollout.policy).Statement : jsonencode(statement)
      if statement.Sid == "LambdaImageRollout"
      ] == [jsonencode({
        Sid      = "LambdaImageRollout"
        Effect   = "Allow"
        Action   = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode"]
        Resource = "arn:aws:lambda:ap-northeast-1:123456789012:function:slice-test-*"
    })]
    error_message = "rolloutのLambda更新は、名前がprefixに従う関数のコード差し替えと読み取りに限定する。"
  }

  assert {
    condition = [
      for statement in jsondecode(aws_iam_role_policy.rollout.policy).Statement : jsonencode(statement)
      if statement.Sid == "BackendImageReadForLambdaRollout"
      ] == [jsonencode({
        Sid      = "BackendImageReadForLambdaRollout"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer"]
        Resource = "arn:aws:ecr:ap-northeast-1:123456789012:repository/slice-test/backend"
    })]
    error_message = "rolloutのECR権限は、backendリポジトリの読み取りに限定する。"
  }

  assert {
    condition = alltrue(flatten([
      for statement in jsondecode(aws_iam_role_policy.rollout.policy).Statement : [
        for action in flatten([statement.Action]) :
        contains(["lambda:ListFunctions", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode"], action)
        if statement.Effect == "Allow" && startswith(action, "lambda:")
      ]
    ]))
    error_message = "rolloutにLambdaの設定変更・作成・削除・実行を渡さない。"
  }
}
