module "runtime_policy" {
  source                = "../modules/runtime-policy"
  account_id            = var.expected_account_id
  region                = local.region
  run_prefix            = local.prefix
  db_resource_id        = aws_db_instance.smoke.resource_id
  master_secret_arn     = aws_db_instance.smoke.master_user_secret[0].secret_arn
  gemini_parameter_path = var.gemini_parameter_path
}
resource "aws_iam_role" "runtime" {
  for_each             = toset(["lambda", "runner", "proxy"])
  name                 = "${local.prefix}-${each.key}"
  path                 = "/vector-test/runtime/"
  permissions_boundary = "arn:aws:iam::${var.expected_account_id}:policy/vector-test/bootstrap/vector-test-${each.key}-boundary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { Service = each.key == "lambda" ? "lambda.amazonaws.com" : "ec2.amazonaws.com" }
    }]
  })
  tags = local.tags
}
resource "aws_iam_role_policy" "runtime" {
  for_each = module.runtime_policy.policies
  name     = "runtime"
  role     = aws_iam_role.runtime[each.key].id
  policy   = each.value
}
resource "aws_iam_instance_profile" "runtime" {
  for_each = toset(["runner", "proxy"])
  name     = "${local.prefix}-${each.key}"
  path     = "/vector-test/runtime/"
  role     = aws_iam_role.runtime[each.key].name
  tags     = local.tags
}
