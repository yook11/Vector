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
    deploy = {
      name = "app-deploy"
      subs = ["repo:${local.repo}:ref:refs/heads/main"]
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
  secret_read_actions = [
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParameterHistory",
    "ssm:GetParametersByPath",
    "secretsmanager:GetSecretValue",
    "kms:Decrypt",
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
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "NoSecretValues"
        Effect   = "Deny"
        Action   = local.secret_read_actions
        Resource = "*"
      },
    ]
  })
}

# --- terraform-apply ------------------------------------------------------

resource "aws_iam_role_policy" "apply" {
  name = "terraform-apply"
  role = aws_iam_role.ci["apply"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
      {
        Sid      = "GlobalInfra"
        Effect   = "Allow"
        Action   = ["route53:*"]
        Resource = "*"
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

      # --- ここから Deny。boundary はこの 4 点が揃って初めて構造になる ---

      # 1. boundary 無しのロール作成を拒否する。
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
            "iam:PermissionsBoundary" = aws_iam_policy.boundary.arn
          }
        }
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
      # 5. ECS 以外への PassRole を拒否する。
      #
      # IamWithinManagedPath の `iam:*` が PassRole を含むため、条件付き Allow を
      # 書くだけでは絞れない (Allow は和集合)。explicit Deny が唯一の手段。
      # iam:PassedToService が無い場合も StringNotEquals は true になるので拒否側に倒れる。
      {
        Sid      = "DenyPassRoleToNonEcs"
        Effect   = "Deny"
        Action   = "iam:PassRole"
        Resource = "*"
        Condition = {
          StringNotEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      },
      # 6. secret の値を読めないようにする。
      #
      # SSM parameter は Terraform で管理しない (aws_ssm_parameter の value は
      # computed なので、ignore_changes を付けても refresh で state に載る)。
      # 「値を読まない」を Deny にすることで、data source をうっかり足しても
      # apply が失敗して気づく。
      {
        Sid      = "NoSecretValues"
        Effect   = "Deny"
        Action   = local.secret_read_actions
        Resource = "*"
      },
    ]
  })
}

# --- app-deploy -----------------------------------------------------------
#
# インフラは触らない。image を push して service を更新するだけ。
# 本丸は ecs:RegisterTaskDefinition と iam:PassRole で、ここを絞らないと
# 「強いロールを渡した task definition を登録する」が通る。

resource "aws_iam_role_policy" "deploy" {
  name = "app-deploy"
  role = aws_iam_role.ci["deploy"].id

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
      {
        Sid    = "EcsRollout"
        Effect = "Allow"
        Action = [
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
      {
        Sid      = "NoSecretValues"
        Effect   = "Deny"
        Action   = local.secret_read_actions
        Resource = "*"
      },
    ]
  })
}
