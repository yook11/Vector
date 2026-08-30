# 用途の違うロールには別の boundary を付ける。天井は「そのロールの policy が
# 壊れたときどこまで届くか」を決めるものなので、全ロールで 1 本にすると天井が
# 全用途の和集合まで広がる。
#
# 分割した boundary はこの Deny を必ず共有する。片方から抜けると、その boundary
# を付けたロールだけ権限昇格に届く。文面を 1 箇所に置いて drift を防ぐ。
locals {
  boundary_no_escalation_statement = {
    Sid    = "NoPrivilegeEscalation"
    Effect = "Deny"
    Action = [
      "iam:*",
      "sts:AssumeRole",
      "organizations:*",
      "account:*",
    ]
    Resource = "*"
  }
}

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
      # elasticache:Connect は接続先 cache と接続 user の両方の ARN に対して
      # 評価されるため、片方だけでは段の policy が許しても認証が通らない。
      {
        Sid    = "ElastiCacheIamAuth"
        Effect = "Allow"
        Action = "elasticache:Connect"
        Resource = [
          "arn:aws:elasticache:${var.region}:${data.aws_caller_identity.current.account_id}:replicationgroup:${var.name_prefix}-*",
          "arn:aws:elasticache:${var.region}:${data.aws_caller_identity.current.account_id}:user:${var.name_prefix}-*",
        ]
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

      # --- chatbot channel role が要るもの ---
      # Slack 通知の alarm グラフ描画に使う読み取り。KMS の例と同じく、
      # 天井にだけ入れておく (boundary の編集は Deny で封印されているため)。
      {
        Sid    = "CloudWatchReadForChatbot"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricWidgetImage",
        ]
        Resource = "*"
      },

      # --- 天井として明示的に落とすもの ---
      # task role は AWS の API をほぼ呼ばないので、IAM も STS も要らない。
      # ここで落としておけば、段の policy を書き間違えても権限昇格に届かない。
      local.boundary_no_escalation_statement,
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

# AgentCore Gateway の service role 専用の天井。
#
# ECS 用の boundary を流用しない。bedrock-agentcore を上の boundary に足すと、
# それを呼ぶ理由の無い task / execution role 16 本の天井まで一緒に上がる。
# 逆にこのロールから見ても、rds-db:connect や ssm:GetParameter が天井に載る
# 理由が無い。信頼元 (bedrock-agentcore.amazonaws.com) も侵害の入口も別なので、
# 天井も分ける。
#
# 中身は本体スタックの aws_iam_role_policy.agentcore_gateway と同じ 2 アクション。
# boundary と policy が同一なのは、このロールの権限が既に必要最小だから。
# 将来 policy を広げるときに、boundary 側も明示的に広げる判断を要求する形になる。
resource "aws_iam_policy" "agentcore_gateway_boundary" {
  name        = "${var.name_prefix}-agentcore-gateway-boundary"
  path        = "/vector-ci/"
  description = "Ceiling for the AgentCore Gateway service role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeGateway"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeGateway"
        Resource = "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/*"
      },
      # web-search.v1 は AWS 所有の tool なので、ARN の account 部が `aws` になる。
      {
        Sid      = "InvokeWebSearch"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeWebSearch"
        Resource = "arn:aws:bedrock-agentcore:${var.region}:aws:tool/web-search.v1"
      },
      local.boundary_no_escalation_statement,
    ]
  })
}
