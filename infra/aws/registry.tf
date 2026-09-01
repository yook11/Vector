# execution role の policy が実 ARN を参照できるよう、pull 元と送り先を先に作る。

# backend は 1 image を 6 段が共有する。段ごとに repo を分けても、同じ image を
# 6 回 push することになるだけで境界は増えない。
#
# IMMUTABLE の帰結: deploy は毎回一意な tag (commit SHA) で push する。
# latest の上書きができないので、push job 側の契約になる。
resource "aws_ecr_repository" "this" {
  for_each = local.images

  name                 = "${var.name_prefix}/${each.value}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${var.name_prefix}-${each.value}" }
}

# 設定しないと古い image が無期限に積み上がって課金され続ける。
#
# ロールバック可能窓 = この保持数。ECS task は再起動のたびに pull するので、
# 失効した image を参照する古い task definition へは戻せない。
resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last ${var.ecr_retained_images} images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_retained_images
        }
        action = { type = "expire" }
      },
    ]
  })
}

# log group は Terraform が作る。
# 帰結: task definition で awslogs-create-group を使わない。使うと execution role に
# logs:CreateLogGroup が要り、boundary (書き込み 2 アクションのみ) で落ちる。
resource "aws_cloudwatch_log_group" "this" {
  for_each = local.stages

  name              = "/ecs/${var.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.name_prefix}-${each.key}" }
}

resource "aws_cloudwatch_log_group" "migration" {
  name              = "/ecs/${var.name_prefix}/migration"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.name_prefix}-migration" }
}
