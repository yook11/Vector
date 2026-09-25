# execution role の policy が実 ARN を参照できるよう、pull 元と送り先を先に作る。

# backend は 1 image を 4 段が共有する。段ごとに repo を分けても、同じ image を
# 4 回 push することになるだけで境界は増えない。
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

# 関数を列挙すると追加時の漏れが遅れて無音で止まるため、名前で許可し関数の作成権限側で絞る。
resource "aws_ecr_repository_policy" "backend_lambda_pull" {
  repository = aws_ecr_repository.this["backend"].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaImageRetrieval"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:aws:lambda:${var.region}:${local.account_id}:function:${var.name_prefix}-*" }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

# Lambdaの版はrolloutが進めるため、ここで決めるのは新規作成時の初期値だけ。
# リポジトリ資源を参照すると読み取りがapplyまで遅延するため、名前を直接組み立てる。
data "aws_ecr_image" "backend_latest" {
  repository_name = "${var.name_prefix}/backend"
  most_recent     = true
}

locals {
  lambda_initial_image_uri = "${aws_ecr_repository.this["backend"].repository_url}@${data.aws_ecr_image.backend_latest.image_digest}"
}

moved {
  from = aws_ecr_repository_policy.outbox_relay
  to   = aws_ecr_repository_policy.backend_lambda_pull
}

# backendは更新頻度が異なるLambdaがdigestを参照し続けるため、自動削除しない。
resource "aws_ecr_lifecycle_policy" "this" {
  for_each = { for name, repository in aws_ecr_repository.this : name => repository if name != "backend" }

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
