# permissions boundary。本体スタックが作る全ロールの権限の天井。
#
# boundary は「作れる権限の上限」なので、これに書いていない権限は、
# 段の policy が何を許していても効かない。段ごとの policy が壊れたり、
# 誰かが AdministratorAccess を付けたりしても、実効権限はこの範囲に留まる。
#
# 仕様上 managed policy でしか作れない (段の policy は inline を使う方針だが、
# boundary だけは例外)。その代わり編集を Deny で封印する (oidc.tf の deny 群)。
resource "aws_iam_policy" "boundary" {
  name        = "${var.name_prefix}-permissions-boundary"
  path        = "/vector-ci/"
  description = "Ceiling for every role created by the main Terraform stack."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # --- task role が要るもの ---
      {
        Sid      = "RdsIamAuth"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:*/*"
      },

      # --- execution role が要るもの ---
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
        Resource = "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/${var.name_prefix}/*"
      },
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/${var.name_prefix}/*"
      },
      {
        Sid    = "ParameterStoreRead"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
        ]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.name_prefix}/*"
      },
      # AWS 管理キー (alias/aws/ssm) を使う限り不要だが、CMK に切り替えたときに
      # boundary の編集 (Deny で封印済み) を要求しないよう天井にだけ入れておく。
      # ViaService で SSM 経由に限定するので、これ単体では何も復号できない。
      {
        Sid      = "KmsDecryptViaSsmOnly"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
        }
      },

      # --- 天井として明示的に落とすもの ---
      # task role は AWS の API をほぼ呼ばないので、IAM も STS も要らない。
      # ここで落としておけば、段の policy を書き間違えても権限昇格に届かない。
      {
        Sid    = "NoPrivilegeEscalation"
        Effect = "Deny"
        Action = [
          "iam:*",
          "sts:AssumeRole",
          "organizations:*",
          "account:*",
        ]
        Resource = "*"
      },
      # ECS Exec を使わない決定を構造で担保する。段の policy に
      # ssmmessages:* を足しても、boundary で落ちるので有効にならない。
      # 使うと決めたときは、この Deny を外す判断が明示的に必要になる。
      {
        Sid      = "NoEcsExec"
        Effect   = "Deny"
        Action   = "ssmmessages:*"
        Resource = "*"
      },
    ]
  })
}
