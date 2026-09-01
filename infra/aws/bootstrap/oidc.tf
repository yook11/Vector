locals {
  account_id = data.aws_caller_identity.current.account_id
  oidc_host  = "token.actions.githubusercontent.com"
  repo       = "${var.github_owner}/${var.github_repo}"

  # 本体スタックが作るロールの path。CI ロールの /vector-ci/ と分けることで、
  # 「apply は自分自身に触れない」を Deny ではなく Allow の欠落として成立させる。
  managed_role_path_arn = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/*"

  ci_scope_arns = [
    "arn:aws:iam::${local.account_id}:role/${var.name_prefix}-ci/*",
    "arn:aws:iam::${local.account_id}:policy/${var.name_prefix}-ci/*",
    "arn:aws:iam::${local.account_id}:oidc-provider/${local.oidc_host}",
  ]

  # /vector-ci/ 配下と OIDC provider に対して禁じる書き込み系。
  # 読み取りは残す (plan の refresh が通るように)。
  iam_write_actions = [
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:UpdateRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:PutRolePermissionsBoundary",
    "iam:DeleteRolePermissionsBoundary",
    "iam:CreatePolicy",
    "iam:CreatePolicyVersion",
    "iam:SetDefaultPolicyVersion",
    "iam:DeletePolicyVersion",
    "iam:DeletePolicy",
    "iam:UpdateOpenIDConnectProviderThumbprint",
    "iam:DeleteOpenIDConnectProvider",
    "iam:AddClientIDToOpenIDConnectProvider",
    "iam:RemoveClientIDFromOpenIDConnectProvider",
  ]

  # 対応表 (boundary.tf) に載っているロールの ARN。ここに無い名前は作れない。
  managed_role_arns = flatten([
    for group in local.role_boundary_groups : [
      for name in group.role_names :
      "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${name}"
    ]
  ])

  # 用途別のロールに、その用途以外の boundary を付けさせない。対応表 1 行につき
  # Deny 1 本。ロール名は完全一致なので、1 つのロールが 2 つの Deny に当たることはない。
  boundary_pairing_statements = [
    for key, group in local.role_boundary_groups : {
      Sid    = "DenyWideBoundaryOn${key}Roles"
      Effect = "Deny"
      Action = "iam:CreateRole"
      Resource = [
        for name in group.role_names :
        "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${name}"
      ]
      Condition = {
        StringNotEquals = {
          "iam:PermissionsBoundary" = group.boundary
        }
      }
    }
  ]

  # CI が assume できるロール。name は「何をするロールか」で付ける
  # (deploy は Terraform を触らないので terraform-* にしない)。
  ci_roles = {
    plan = {
      name = "terraform-plan"
      subs = ["repo:${local.repo}:pull_request", "repo:${local.repo}:ref:refs/heads/main"]
    }
    apply = {
      name = "terraform-apply"
      subs = ["repo:${local.repo}:environment:${var.deploy_environment}"]
    }
    # image を焼く工程と本番を入れ替える工程で role を分ける。build は Dockerfile と
    # 依存パッケージのコードが実際に走る場所なので、そこに本番差し替えの権限を持たせない。
    push = {
      name = "app-push"
      subs = ["repo:${local.repo}:ref:refs/heads/main"]
    }
    # sub を environment 限定にすることで、承認を経ない job には token 自体が
    # 発行されなくなる。承認ゲートが運用の約束ではなく経路の不在になる。
    rollout = {
      name = "app-rollout"
      subs = ["repo:${local.repo}:environment:${var.deploy_environment}"]
    }
    migrate = {
      name = "db-migrate"
      subs = ["repo:${local.repo}:environment:${var.deploy_environment}"]
    }
  }

  # IAM Identity Center が permission set ごとに member アカウントへ作るロール。
  # 末尾の接尾辞は割り当てを作り直すたびに変わるため ARN を完全一致で書けない。
  # path の region 部分は IdC インスタンスのリージョンで、ここでは var.region と一致する。
  sso_deploy_role_pattern = join("/", [
    "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com",
    var.region,
    "AWSReservedSSO_${var.deploy_permission_set}_*",
  ])

  # secret の実体を CI が読めないようにする。
  # 「Terraform から SSM の値を読まない」を運用の約束ではなく IAM の Deny にする。
  #
  # SSM だけ Resource を自アカウント所有の parameter に絞る。AWS が公開する
  # /aws/service/* (最新 AMI の ID など) は **account 部が空の ARN** で、`*` で
  # 巻き込むと bastion の AMI 参照が Deny で落ちる。秘密は全て自アカウントの
  # parameter にあるので、絞っても射程は変わらない。
  secret_read_statements = [
    {
      Sid    = "NoOwnParameterValues"
      Effect = "Deny"
      Action = [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParameterHistory",
        "ssm:GetParametersByPath",
      ]
      Resource = "arn:aws:ssm:*:${local.account_id}:parameter/*"
    },
    {
      Sid    = "NoSecretValues"
      Effect = "Deny"
      Action = [
        "secretsmanager:GetSecretValue",
        "kms:Decrypt",
      ]
      Resource = "*"
    },
  ]
}

# thumbprint_list は指定しない (provider schema 上 optional + computed)。
# GitHub の OIDC は AWS 側が信頼された CA で検証するようになっており、
# 手で thumbprint を固定すると更新のたびに壊れる。
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://${local.oidc_host}"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = null
}

# CI ロールの受け入れ名簿。GitHub Actions と、人間が通る permission set の 2 経路。
# **どちらの経路も同じ CI ロールになる**ので、デプロイで何ができるかの定義は
# 下の policy 群 1 箇所に留まる。
data "aws_iam_policy_document" "ci_role_trust" {
  for_each = local.ci_roles

  # sub claim は完全一致で書く。`repo:owner/repo:*` のようなワイルドカードにすると
  # 「どのブランチからでも assume できるロール」になる。
  statement {
    sid     = "GitHubActions"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = each.value.subs
    }
  }

  # principals に ARN のワイルドカードは書けないため、入口はアカウントにして
  # 実際の絞り込みを condition で行う (AWS が案内している permission set の書き方)。
  # 両者は AND なので、実効的に通るのは deploy permission set のロールだけ。
  statement {
    sid     = "DeployPermissionSet"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values   = [local.sso_deploy_role_pattern]
    }
  }
}

resource "aws_iam_role" "ci" {
  for_each = local.ci_roles

  name               = "${var.name_prefix}-ci-${each.value.name}"
  path               = "/${var.name_prefix}-ci/"
  assume_role_policy = data.aws_iam_policy_document.ci_role_trust[each.key].json
}

# --- terraform-plan -------------------------------------------------------
#
# 世界は読めるが secret の値は読めない。state の lock を取らないので
# CI では `terraform plan -lock=false` で走らせる (native locking は state
# bucket に lock オブジェクトを書くため、read-only では失敗する)。

resource "aws_iam_role_policy_attachment" "plan_read_only" {
  role       = aws_iam_role.ci["plan"].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "plan_deny_secret_read" {
  name = "deny-secret-read"
  role = aws_iam_role.ci["plan"].id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.secret_read_statements
  })
}

# --- terraform-apply ------------------------------------------------------

resource "aws_iam_role_policy" "apply" {
  name = "terraform-apply"
  role = aws_iam_role.ci["apply"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      # 本体スタックが触るリージョナルサービス。
      #
      # ここを action 単位で列挙しないのは意図的。greenfield のスタックで
      # 全 action を数え上げると、抜けるたびに apply が落ちて後から広げる
      # 運用になり、結局 * に近づく。実効的な境界は下の Deny 群と、
      # IAM を /vector/ path に閉じ込めたことで作る。
      {
        Sid    = "RegionalInfra"
        Effect = "Allow"
        Action = [
          "ec2:*",
          "rds:*",
          "elasticache:*",
          "ecs:*",
          "ecr:*",
          "elasticloadbalancing:*",
          "logs:*",
          "servicediscovery:*",
          "acm:*",
          "application-autoscaling:*",
          # 監視・アラート基盤 (alerting.tf): topic / alarm / rule の管理。
          "sns:*",
          "cloudwatch:*",
          "events:*",
          # agent の外部検索が使う AgentCore Gateway と web-search connector
          # (agentcore.tf)。Gateway / GatewayTarget の CRUD が要る。
          "bedrock-agentcore:*",
          "ssm:DescribeParameters",
          "secretsmanager:DescribeSecret",
          "kms:DescribeKey",
          "kms:ListAliases",
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
      # Route 53 はグローバル。region 条件を付けると通らない。
      # chatbot は API endpoint が限られた region (us-east-2 等) にしか無く、
      # aws:RequestedRegion が var.region に一致しないためここに置く。
      {
        Sid      = "GlobalInfra"
        Effect   = "Allow"
        Action   = ["route53:*", "chatbot:*"]
        Resource = "*"
      },
      # AWS が公開する最新 AMI の ID (bastion.tf の data source が読む)。
      # account 部が空の ARN なので、自アカウントの parameter を落とす Deny とは
      # 重ならない。秘密を含まない値だけがこの namespace に居る。
      {
        Sid    = "PublicServiceParameters"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
        ]
        Resource = "arn:aws:ssm:${var.region}::parameter/aws/service/*"
      },
      {
        Sid    = "StateBackend"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.tfstate.arn,
          "${aws_s3_bucket.tfstate.arn}/*",
        ]
      },
      # IAM の書き込みは /vector/ path の中だけ。
      # /vector-ci/ (CI ロールと boundary) には Allow が届かない。
      {
        Sid      = "IamWithinManagedPath"
        Effect   = "Allow"
        Action   = "iam:*"
        Resource = local.managed_role_path_arn
      },
      {
        Sid      = "IamRead"
        Effect   = "Allow"
        Action   = ["iam:Get*", "iam:List*"]
        Resource = "*"
      },
      # ECS service / task definition に渡せるのは /vector/ のロールだけ。
      # ここを絞らないと「既存の強いロールをサービスに渡す」経路が残る。
      #
      # 注意: この Allow だけでは絞れない。上の IamWithinManagedPath が
      # `iam:*` なので iam:PassRole も含んでしまい、Allow は和集合なので
      # 条件付き Allow を足しても広い方が勝つ。実際に効かせているのは
      # 下の DenyPassRoleToNonEcs (explicit Deny)。
      # simulate-principal-policy で ec2 への PassRole が allowed になって発覚した。
      {
        Sid      = "PassRoleToEcsOnly"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = local.managed_role_path_arn
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },

      # --- ここから Deny。boundary はこの 6 点が揃って初めて構造になる ---

      # 1. 想定した boundary 以外でのロール作成を拒否する。
      #
      # boundary を用途別に分けたので、ここは許可リストになる。リストに
      # 載せた時点で「CI はこの中から選べる」という意味になり、用途の違う天井
      # (task role に execution の天井など) を選べてしまう。それを防ぐのが 1b + 1c。
      #
      # 1b / 1c があれば CreateRole については冗長だが、iam:CreateUser を
      # 押さえているのと、対応表を間違えても「boundary 無しは通さない」を
      # 独立に保証するので残す。
      {
        Sid    = "DenyRoleCreationWithoutBoundary"
        Effect = "Deny"
        Action = [
          "iam:CreateRole",
          "iam:CreateUser",
        ]
        Resource = "*"
        Condition = {
          StringNotEquals = {
            "iam:PermissionsBoundary" = [for group in local.role_boundary_groups : group.boundary]
          }
        }
      },
      # 1b. 対応表に無い名前のロールを作らせない。
      #
      # これが無いと、別名のロールを作って用途別 boundary をすり抜けられる。
      # 1c は「このロールにはこの天井」を縛るだけなので、表に載っていない名前は
      # どの Deny にも当たらず素通りしてしまう。
      #
      # 段を増やすときは boundary.tf の対応表を先に apply する。忘れると
      # ここで CreateRole が落ちる (天井を決めずに段が増えないための順序)。
      {
        Sid         = "DenyRoleCreationOutsideKnownRoles"
        Effect      = "Deny"
        Action      = "iam:CreateRole"
        NotResource = local.managed_role_arns
      },
      # 2. 作った後で boundary を外す / 差し替えるのを拒否する。
      {
        Sid    = "DenyBoundaryTampering"
        Effect = "Deny"
        Action = [
          "iam:PutRolePermissionsBoundary",
          "iam:DeleteRolePermissionsBoundary",
          "iam:PutUserPermissionsBoundary",
          "iam:DeleteUserPermissionsBoundary",
        ]
        Resource = "*"
      },
      # 3 + 4. boundary policy 自身、CI ロール、OIDC provider への書き込みを拒否する。
      #
      # 3 が抜けると「boundary は必ず付くが、その中身を Administrator に
      # 書き換える」が通る。4 が抜けると「自分の Deny を自分で消す」が通り、
      # Deny 全体が運用の約束に退化する。
      # Allow 側 (IamWithinManagedPath) が /vector/ に閉じているので本来は
      # 届かないが、二重底として明示する。
      {
        Sid      = "DenyTouchingCiScope"
        Effect   = "Deny"
        Action   = local.iam_write_actions
        Resource = local.ci_scope_arns
      },
      # 5. 意図したサービス以外への PassRole を拒否する。
      #    許すのは ECS (task / execution role)、Chatbot (Slack 通知の channel role)、
      #    AgentCore (Gateway の service role) だけ。
      #
      # IamWithinManagedPath の `iam:*` が PassRole を含むため、条件付き Allow を
      # 書くだけでは絞れない (Allow は和集合)。explicit Deny が唯一の手段。
      # iam:PassedToService が無い場合も StringNotEquals は true になるので拒否側に倒れる。
      {
        Sid      = "DenyPassRoleToUnintendedServices"
        Effect   = "Deny"
        Action   = "iam:PassRole"
        Resource = "*"
        Condition = {
          StringNotEquals = {
            "iam:PassedToService" = [
              "ecs-tasks.amazonaws.com",
              "chatbot.amazonaws.com",
              "bedrock-agentcore.amazonaws.com",
            ]
          }
        }
      },
      # 5b. AgentCore へ渡せるのは gateway service role 1 本だけ。
      #
      # ECS のように「/vector/ 配下なら何でも」にはしない。CI は
      # IamWithinManagedPath で /vector/ 配下のロールを作成・変更できるため、
      # prefix 一致にすると「CI が強い権限のロールを作って AgentCore へ渡す」
      # 経路が開く。名前を 1 本に固定して、その経路を塞ぐ。
      #
      # ロール名は本体スタックの aws_iam_role.agentcore_gateway と対応する。
      # bootstrap から本体の local は参照できないので二重管理になる。名前を
      # 変えるときは両方直す (片方だけだと apply が PassRole で落ちて気づく)。
      {
        Sid         = "DenyPassRoleToAgentCoreExceptGateway"
        Effect      = "Deny"
        Action      = "iam:PassRole"
        NotResource = "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-agentcore-gateway"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "bedrock-agentcore.amazonaws.com"
          }
        }
      },
      # 6. secret の値を読めないようにする。
      #
      # SSM parameter は Terraform で管理しない (aws_ssm_parameter の value は
      # computed なので、ignore_changes を付けても refresh で state に載る)。
      # 「値を読まない」を Deny にすることで、data source をうっかり足しても
      # apply が失敗して気づく。
      #
      # 1c は用途別のロールに、その用途以外の boundary を付けさせない Deny。
      # boundary.tf の local.role_boundary_groups から 1 行につき 1 本生成する。
    ], local.boundary_pairing_statements, local.secret_read_statements)
  })
}

# --- app-push -------------------------------------------------------------
#
# ECR に image を置くだけ。ECS には一切届かない。この role で焼いた image が
# 本番に載るには、必ず承認を通る app-rollout が要る。

resource "aws_iam_role_policy" "push" {
  name = "app-push"
  role = aws_iam_role.ci["push"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = "arn:aws:ecr:${var.region}:${local.account_id}:repository/${var.name_prefix}/*"
      },
    ], local.secret_read_statements)
  })
}

# --- app-rollout ----------------------------------------------------------
#
# 本番の service を入れ替えるだけ。image は焼かないので ECR への書き込みは持たない。
# 本丸は ecs:RegisterTaskDefinition と iam:PassRole で、ここを絞らないと
# 「強いロールを渡した task definition を登録する」が通る。

resource "aws_iam_role_policy" "rollout" {
  name = "app-rollout"
  role = aws_iam_role.ci["rollout"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "EcsRollout"
        Effect = "Allow"
        Action = [
          # 入れ替え対象は cluster に問い合わせて数える。workflow 側に段の一覧を
          # 持つと locals.tf と 2 箇所になり、段の追加が黙って漏れる。
          "ecs:ListServices",
          "ecs:DescribeServices",
          "ecs:DescribeTaskDefinition",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:RegisterTaskDefinition",
          "ecs:UpdateService",
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
      {
        Sid      = "PassRoleToEcsOnly"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = local.managed_role_path_arn
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ], local.secret_read_statements)
  })
}

# --- db-migrate -----------------------------------------------------------

resource "aws_iam_role_policy" "migrate" {
  name = "db-migrate"
  role = aws_iam_role.ci["migrate"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "RegisterMigrationTaskDefinition"
        Effect   = "Allow"
        Action   = "ecs:RegisterTaskDefinition"
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/VectorPurpose" = "migration"
            "ecs:compute-compatibility"    = "FARGATE"
            "ecs:privileged"               = "false"
          }
          NumericEquals = {
            "ecs:task-cpu"    = 256
            "ecs:task-memory" = 512
          }
          Null = {
            "aws:RequestTag/ReleaseSha"  = "false"
            "aws:RequestTag/GitHubRunId" = "false"
          }
          "ForAllValues:StringEquals" = {
            "aws:TagKeys" = ["VectorPurpose", "ReleaseSha", "GitHubRunId"]
          }
        }
      },
      {
        Sid      = "RunMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${var.name_prefix}-migration:*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/VectorPurpose" = "migration"
            "ecs:cluster"                  = "arn:aws:ecs:${var.region}:${local.account_id}:cluster/${var.name_prefix}"
            "ecs:enable-execute-command"   = "false"
          }
          Null = {
            "aws:RequestTag/ReleaseSha"  = "false"
            "aws:RequestTag/GitHubRunId" = "false"
          }
          "ForAllValues:StringEquals" = {
            "aws:TagKeys" = ["VectorPurpose", "ReleaseSha", "GitHubRunId"]
          }
        }
      },
      {
        Sid    = "TagMigrationResourcesOnlyAtCreation"
        Effect = "Allow"
        Action = "ecs:TagResource"
        Resource = [
          "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${var.name_prefix}-migration:*",
          "arn:aws:ecs:${var.region}:${local.account_id}:task/${var.name_prefix}/*",
        ]
        Condition = {
          StringEquals = {
            "aws:RequestTag/VectorPurpose" = "migration"
            "ecs:CreateAction" = [
              "RegisterTaskDefinition",
              "RunTask",
            ]
          }
          "ForAllValues:StringEquals" = {
            "aws:TagKeys" = ["VectorPurpose", "ReleaseSha", "GitHubRunId"]
          }
        }
      },
      {
        Sid      = "StopTaggedMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:StopTask"
        Resource = "arn:aws:ecs:${var.region}:${local.account_id}:task/${var.name_prefix}/*"
        Condition = {
          StringEquals = {
            "aws:ResourceTag/VectorPurpose" = "migration"
            "ecs:cluster"                   = "arn:aws:ecs:${var.region}:${local.account_id}:cluster/${var.name_prefix}"
          }
        }
      },
      {
        Sid    = "InspectMigrationTasks"
        Effect = "Allow"
        Action = [
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:DescribeTaskDefinition",
          "ecs:ListTagsForResource",
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
      {
        Sid      = "DiscoverMigrationNetwork"
        Effect   = "Allow"
        Action   = ["ec2:DescribeSecurityGroups", "ec2:DescribeSubnets"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
      {
        Sid    = "PassMigrationRolesToEcsOnly"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-migration-task",
          "arn:aws:iam::${local.account_id}:role/${var.name_prefix}/${var.name_prefix}-migration-exec",
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        Sid      = "HumanIamConnectionAsOwner"
        Effect   = "Allow"
        Action   = "rds-db:connect"
        Resource = "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:*/vector"
      },
    ], local.secret_read_statements)
  })
}
