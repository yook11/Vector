# permissions boundary。本体スタックが作るロールの権限の天井。
#
# boundary は「作れる権限の上限」なので、これに書いていない権限は、
# そのロールの policy が何を許していても効かない。policy が壊れたり、
# 誰かが AdministratorAccess を付けたりしても、実効権限はこの範囲に留まる。
#
# **用途ごとに分ける。** 天井は「そのロールの policy が壊れたときどこまで届くか」を
# 決めるものなので、1 本に統合すると天井が全用途の和集合まで広がる。統合していた頃は
# task role の天井に parameter/vector/* の ssm:GetParameter (= 全段の secret) が
# 載っていた。task role は container credentials endpoint から読める唯一の資格情報で、
# アプリの RCE / SSRF が最初に手に入れるものなのに、である。
#
# ただし分割だけでは制御にならない。oidc.tf の DenyRoleCreationWithoutBoundary は
# boundary の許可リストになるため、ロール名と boundary を対で縛って初めて
# 天井の縮小として効く (local.role_boundary_groups)。
#
# 仕様上 managed policy でしか作れない (ロールの policy は inline を使う方針だが、
# boundary だけは例外)。その代わり編集を Deny で封印する (oidc.tf の deny 群)。

locals {
  # 分割した boundary が共通で持つ天井。片方から抜けると、その boundary を
  # 付けたロールだけ権限昇格に届く。文面を 1 箇所に置いて drift を防ぐ。
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

  # ECS Exec を使わない決定を構造で担保する。ロールの policy に ssmmessages:* を
  # 足しても、boundary で落ちるので有効にならない。使うと決めたときは、この Deny を
  # 外す判断が明示的に必要になる。
  #
  # task 系の boundary にだけ置く。ECS Exec が ssmmessages を要求するのは
  # task role であって execution role ではない。
  boundary_no_ecs_exec_statement = {
    Sid      = "NoEcsExec"
    Effect   = "Deny"
    Action   = "ssmmessages:*"
    Resource = "*"
  }

  # IAM auth の入口 2 アクション。task 系の boundary が共有する。
  #
  # これだけで済むのは偶然ではなくキュー選定の帰結で、SQS を採っていれば
  # sqs:* が段ごとに載っていた。Valkey を選んだ時点で、権限設計の重心が
  # IAM から Redis ACL と Postgres の GRANT へ移った。
  task_data_plane_statements = [
    {
      Sid      = "RdsIamAuth"
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "arn:aws:rds-db:${var.region}:${data.aws_caller_identity.current.account_id}:dbuser:*/*"
    },
    # elasticache:Connect は接続先 cache と接続 user の両方の ARN に対して
    # 評価されるため、片方だけではロールの policy が許しても認証が通らない。
    {
      Sid    = "ElastiCacheIamAuth"
      Effect = "Allow"
      Action = "elasticache:Connect"
      Resource = [
        "arn:aws:elasticache:${var.region}:${data.aws_caller_identity.current.account_id}:replicationgroup:${var.name_prefix}-*",
        "arn:aws:elasticache:${var.region}:${data.aws_caller_identity.current.account_id}:user:${var.name_prefix}-*",
      ]
    },
  ]

  # 本体スタックが作るロールと、その天井の対応。
  # **ここに無い名前のロールは作れない** (oidc.tf の DenyRoleCreationOutsideKnownRoles)。
  #
  # wildcard ではなく完全列挙にしている。vector-agent-task は vector-*-task にも
  # 当たるため pattern だと 2 つの Deny を踏んで作成不能になり、そもそも pattern では
  # 「表に無い名前を拒否」を表現できない。
  #
  # bootstrap は本体の local.stages を参照できないので段名をここでも持つ。段を増やす
  # ときは、この表を apply してから本体を apply する。順序を守らないと CreateRole が
  # Deny で落ちる。天井を決めずに段が増えないようにするための順序。
  role_boundary_groups = {
    Task = {
      boundary = aws_iam_policy.task_boundary.arn
      role_names = [
        for s in ["frontend", "api", "scheduler", "fetch", "analysis", "insights", "proxy"] :
        "${var.name_prefix}-${s}-task"
      ]
    }
    # agent 段だけが外部検索で gateway を呼ぶ (本体の
    # aws_iam_role_policy.agentcore_gateway_invoke)。天井もそこだけに限定する。
    AgentTask = {
      boundary   = aws_iam_policy.agent_task_boundary.arn
      role_names = ["${var.name_prefix}-agent-task"]
    }
    Execution = {
      boundary = aws_iam_policy.execution_boundary.arn
      role_names = [
        for s in ["frontend", "api", "scheduler", "fetch", "analysis", "insights", "agent", "proxy"] :
        "${var.name_prefix}-${s}-exec"
      ]
    }
    Chatbot = {
      boundary   = aws_iam_policy.chatbot_boundary.arn
      role_names = ["${var.name_prefix}-chatbot"]
    }
    AgentCore = {
      boundary   = aws_iam_policy.agentcore_gateway_boundary.arn
      role_names = ["${var.name_prefix}-agentcore-gateway"]
    }
  }
}

# コンテナ内のアプリケーションが使うロールの天井。
#
# 4 種のうち唯一、コンテナから資格情報を読み出せる。侵害の入口はアプリ自身
# (RCE / SSRF) なので、天井が最も狭くあるべきものになる。
resource "aws_iam_policy" "task_boundary" {
  name        = "${var.name_prefix}-task-boundary"
  path        = "/vector-ci/"
  description = "Ceiling for ECS task roles (the credential reachable from inside the container)."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.task_data_plane_statements, [
      local.boundary_no_escalation_statement,
      local.boundary_no_ecs_exec_statement,
    ])
  })
}

# agent 段の task role だけの天井。上に gateway 呼び出しを 1 つ足したもの。
#
# 共通の task boundary には入れない。外部検索を持たない 7 段の天井まで
# 上げる理由が無い。
resource "aws_iam_policy" "agent_task_boundary" {
  name        = "${var.name_prefix}-agent-task-boundary"
  path        = "/vector-ci/"
  description = "Ceiling for the agent stage task role (task boundary plus the web-search gateway)."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.task_data_plane_statements, [
      # gateway の ID は apply 時に確定するので、天井は gateway/* で受ける。
      # 実際にどの gateway を呼べるかは本体の policy が ARN で名指しする。
      {
        Sid      = "InvokeWebSearchGateway"
        Effect   = "Allow"
        Action   = "bedrock-agentcore:InvokeGateway"
        Resource = "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/*"
      },
      local.boundary_no_escalation_statement,
      local.boundary_no_ecs_exec_statement,
    ])
  })
}

# ECS / Fargate がコンテナを起動・維持するために使うロールの天井。
#
# secret の注入も image の pull もこちら側で、コンテナには渡らない。
# 侵害の入口は task definition の書き換え (= ECS への書き込み権限) であって、
# アプリの侵害ではない。task boundary と分ける理由がここにある。
resource "aws_iam_policy" "execution_boundary" {
  name        = "${var.name_prefix}-execution-boundary"
  path        = "/vector-ci/"
  description = "Ceiling for ECS execution roles (image pull and secret injection)."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
      local.boundary_no_escalation_statement,
    ]
  })
}

# Slack 通知の channel role の天井。alarm グラフ描画に使う読み取りだけ。
resource "aws_iam_policy" "chatbot_boundary" {
  name        = "${var.name_prefix}-chatbot-boundary"
  path        = "/vector-ci/"
  description = "Ceiling for the AWS Chatbot channel role."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
      local.boundary_no_escalation_statement,
    ]
  })
}

# AgentCore Gateway の service role の天井。
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

# 分割前の統合 boundary。本体スタックの差し替えが終わるまで残す。
#
# 消すのは、17 本のロールが上の boundary へ移り終わってから (attach 中の policy は
# 削除できない)。許可リストからこれを外して初めて分割が完成するので、
# 削除は cleanup ではなくこの作業の一部になる。
#
# description は触らない。IAM は policy の description を更新できず、terraform が
# **再作成** (= 17 ロールに attach 中の policy を destroy) に倒す。
resource "aws_iam_policy" "boundary" {
  name        = "${var.name_prefix}-permissions-boundary"
  path        = "/vector-ci/"
  description = "Ceiling for every role created by the main Terraform stack."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.task_data_plane_statements, [
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
      local.boundary_no_escalation_statement,
      local.boundary_no_ecs_exec_statement,
    ])
  })
}
