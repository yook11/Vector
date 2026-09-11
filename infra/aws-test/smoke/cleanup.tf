resource "terraform_data" "lambda_eni_cleanup" {
  input = {
    expected_account_id = var.expected_account_id
    aws_profile         = var.aws_profile
    region              = local.region
    subnet_id           = aws_subnet.smoke["lambda"].id
    security_group_id   = aws_security_group.smoke["lambda"].id
  }

  # 関数削除後のENI消滅を待ち、Lambdaの実行権限とネットワークを先に削除させない。
  depends_on = [aws_iam_role_policy.runtime]

  provisioner "local-exec" {
    when    = destroy
    command = "python3 ../scripts/wait-lambda-eni-deletion.py"
    environment = {
      VECTOR_ENI_CLEANUP = jsonencode(self.input)
    }
  }
}
