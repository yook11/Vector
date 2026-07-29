# 段ごとに task role と execution role を 1 つずつ。計 14。
#
# なぜ execution role まで段ごとに分けるか: ECS の secret 注入は
# コンテナ起動前に **execution role** が行う (task role ではない)。共有すると
# 「全段の secret を読める role が 1 つ存在する」状態になり、段の分離がそこで崩れる。

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# ECS が task を起動するときに引き受ける。confused deputy 対策として
# 呼び出し元アカウントと ECS の ARN を条件に入れる。
#
# SourceArn は cluster ARN まで絞らない。AssumeRole 時の実体は task ARN で、
# task ID は起動のたびに変わる。AWS のサンプルどおり region + account までに留める
# (絞りすぎると全 task が起動しなくなる)。
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${var.region}:${local.account_id}:*"]
    }
  }
}

# --- task role ------------------------------------------------------------
#
# このアプリは実行時に AWS の API を呼ばない。LLM は外部 SaaS、キューは Valkey、
# ログは Logfire、ストレージ無し。よって task role に載るのは IAM DB auth の
# 1 アクションだけになる。
#
# これは偶然ではなくキュー選定の帰結。SQS を採っていれば段ごとに
# sqs:SendMessage / ReceiveMessage / DeleteMessage が載り、IAM が主戦場のままだった。
# Valkey を選んだ時点で、権限設計の重心が IAM から Redis ACL と Postgres の GRANT へ移った。
resource "aws_iam_role" "task" {
  for_each = local.stages

  name                 = "${var.name_prefix}-${each.key}-task"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = var.permissions_boundary_arn
}

# scheduler には policy を付けない。cron を発火するだけで DB に触らないため、
# **権限がゼロの task role** になる。空であること自体が棚卸しの結論。
resource "aws_iam_role_policy" "task" {
  for_each = local.db_stages

  name = "rds-iam-auth"
  role = aws_iam_role.task[each.value].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RdsIamAuth"
        Effect = "Allow"
        Action = "rds-db:connect"
        # ARN が Postgres の user 名を名指しする。IAM が決めるのは
        # 「どの入口に入れるか」だけで、入った後に何ができるかは
        # migration の GRANT が全部決める。
        # DbiResourceId (db-XXXXXXXX) であって instance identifier ではない。
        # スナップショットから復元すると変わるので、必ず resource_id を参照する。
        Resource = [
          for user in local.stages[each.value].db_users :
          "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/${user}"
        ]
      },
    ]
  })
}

# --- execution role -------------------------------------------------------

resource "aws_iam_role" "execution" {
  for_each = local.stages

  name                 = "${var.name_prefix}-${each.key}-exec"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_iam_role_policy" "execution" {
  for_each = local.stages

  name = "ecs-task-execution"
  role = aws_iam_role.execution[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # resource を絞れないが、絞る必要もない。発行されるのは registry への
      # ログイン用一時トークンで、それ自体は何も取得できない。実際に何を pull
      # できるかは下の EcrPull が決める。
      {
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.this[each.value.image].arn
      },
      # 両形を並べる。provider の .arn は API が付ける :* を落とすので、
      # ここでの式は log-group:/ecs/vector/<段>:* になる。AWS の
      # Service Authorization Reference 上 log-group の ARN 形式は :* 付きなので
      # 本来はこれで足りるが、CreateLogStream が接尾辞なしで評価される実装差の
      # 報告があり、外すと awslogs driver (既定 blocking) が log stream を
      # 作れず **task が起動しない**。両方書くコストはゼロなので保険を取る。
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.this[each.key].arn,
          "${aws_cloudwatch_log_group.this[each.key].arn}:*",
        ]
      },
      # secret は Terraform で管理しない (aws_ssm_parameter の value は computed で、
      # refresh のたびに実値が state に載るため)。値も箱も CLI で作り、ここでは
      # path で参照するだけ。段の分離は「どの path を読めるか」で表現する。
      {
        Sid    = "ParameterStoreRead"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
        ]
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/${each.key}/*"
      },
    ]
  })
}
