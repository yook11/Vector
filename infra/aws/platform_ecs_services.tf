# ECS サービスの宣言。subnet・security group・IAM role・Valkey user はこの表から生成する。
# subnet の番号だけは platform_network.tf の subnet_indexes が持つ。
locals {
  # 各列はそのサービスが実際に使うものだけを書く。
  # bff-jwt-signing / revalidate-bearer の secret だけは、Settings が構築時に必須とするため backend の全サービスに配る。
  # scheduler は cron を発火するだけで DB に接続しないので db_users が空。
  services = {
    frontend = {
      needs_broker   = false
      egress_vendors = [], egress_allow_any_domain = false
      image          = "frontend", db_users = ["vector_auth"]
      cpu            = 256, memory = 1024, port = 3000, singleton = false
      command        = []
      secrets = {
        BETTER_AUTH_SECRET       = "better-auth-secret"
        BFF_JWT_SIGNING_SECRET   = "bff-jwt-signing-secret"
        REVALIDATE_BEARER_SECRET = "revalidate-bearer-secret"
      }
    }
    api = {
      needs_broker   = true
      egress_vendors = ["logfire"], egress_allow_any_domain = false
      image          = "backend", db_users = ["vector_api"]
      cpu            = 256, memory = 512, port = 8000, singleton = false
      command        = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
      secrets = {
        # research 開始 API の事前チェックが key の存在だけを見る。呼び出しは agent が行うので api は外へ出ない。
        DEEPSEEK_API_KEY         = "deepseek-api-key"
        TAVILY_API_KEY           = "tavily-api-key"
        BFF_JWT_SIGNING_SECRET   = "bff-jwt-signing-secret"
        REVALIDATE_BEARER_SECRET = "revalidate-bearer-secret"
        LOGFIRE_TOKEN            = "logfire-token"
      }
    }
    # singleton: 新旧が並走すると cron が二重発火するので 1 task に保つ。
    scheduler = {
      needs_broker   = true
      egress_vendors = ["logfire"], egress_allow_any_domain = false
      image          = "backend", db_users = []
      cpu            = 256, memory = 512, port = null, singleton = true
      command        = ["supervisord", "-n", "-c", "/app/supervisord/scheduler.conf"]
      secrets = {
        BFF_JWT_SIGNING_SECRET   = "bff-jwt-signing-secret"
        REVALIDATE_BEARER_SECRET = "revalidate-bearer-secret"
        LOGFIRE_TOKEN            = "logfire-token"
      }
    }
    insights = {
      needs_broker   = true
      egress_vendors = ["deepseek", "logfire"], egress_allow_any_domain = false
      image          = "backend", db_users = ["vector_insights"]
      cpu            = 256, memory = 1024, port = null, singleton = false
      command        = ["supervisord", "-n", "-c", "/app/supervisord/insights.conf"]
      secrets = {
        DEEPSEEK_API_KEY         = "deepseek-api-key"
        BFF_JWT_SIGNING_SECRET   = "bff-jwt-signing-secret"
        REVALIDATE_BEARER_SECRET = "revalidate-bearer-secret"
        LOGFIRE_TOKEN            = "logfire-token"
      }
    }
    agent = {
      needs_broker   = true
      egress_vendors = ["deepseek", "gemini", "tavily", "logfire"], egress_allow_any_domain = false
      image          = "backend", db_users = ["vector_agent"]
      cpu            = 256, memory = 1024, port = null, singleton = false
      command        = ["supervisord", "-n", "-c", "/app/supervisord/agent.conf"]
      secrets = {
        GEMINI_API_KEY           = "gemini-api-key"
        DEEPSEEK_API_KEY         = "deepseek-api-key"
        TAVILY_API_KEY           = "tavily-api-key"
        BFF_JWT_SIGNING_SECRET   = "bff-jwt-signing-secret"
        REVALIDATE_BEARER_SECRET = "revalidate-bearer-secret"
        LOGFIRE_TOKEN            = "logfire-token"
      }
    }
  }

  # resource の cidr_block から読み戻すと plan 時に unknown になり squid.conf の差分が読めないため、locals で確定させる。
  app_subnet_cidrs = { for name in keys(local.services) : name => local.subnet_cidrs[name] }

  all_services    = toset(keys(local.services))
  db_services     = toset([for name, s in local.services : name if length(s.db_users) > 0])
  broker_services = toset([for name, s in local.services : name if s.needs_broker])
  egress_services = toset([
    for name, s in local.services : name
    if length(s.egress_vendors) > 0 || s.egress_allow_any_domain
  ])

  # 内部から名前で呼ばれるサービスだけ Cloud Map に登録する。
  # frontend → api (BFF) と insights → frontend (revalidate) の 2 経路だけ。
  discoverable_services = toset(["frontend", "api"])
  internal_api_url      = "http://api.${var.internal_namespace}:8000/api/v1"
  internal_frontend_url = "http://frontend.${var.internal_namespace}:3000"
}

resource "aws_ecs_cluster" "this" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

locals {
  common_environment = {
    ENV        = "production"
    AWS_REGION = var.region
    # Settings が構築時に必須とするため全サービスに配る (実際に使うのは一部だけ)。
    FRONTEND_URL           = "https://${var.frontend_domain}"
    CROSSREF_CONTACT_EMAIL = var.crossref_contact_email
    # IAM 認証は明示フラグで有効にする。password の有無で推測すると、設定漏れが黙って IAM モードになる。
    DB_IAM_AUTH    = "true"
    REDIS_IAM_AUTH = "true"
    # token の署名に使う cache 名 (URL から導出できない)。broker ノードの名前なので rate-limit 側は読まない。
    REDIS_IAM_CACHE_NAME       = aws_elasticache_replication_group.broker.replication_group_id
    INTERNAL_FRONTEND_BASE_URL = local.internal_frontend_url
    # ECS の credential endpoint を外すと、SDK の資格情報取得が proxy に迂回して失敗する。
    NO_PROXY = join(",", [
      "169.254.169.254",
      "169.254.170.2",
      ".${var.internal_namespace}",
      # AgentCore Gateway は PrivateLink 経由の内部宛先。
      local.agentcore_gateway_host,
    ])
    # 外向き proxy の使われ方は 3 通り。SDK (DeepSeek / Gemini / Logfire) は HTTPS_PROXY を読み、
    # backend の第三者宛 client は EGRESS_PROXY_URL を明示で使い、内部宛 client は proxy を通さない。
    # frontend にも入るが Node は既定で読まない。読むライブラリを入れたら service_environment へ移す。
    HTTPS_PROXY      = local.proxy_url
    HTTP_PROXY       = local.proxy_url
    EGRESS_PROXY_URL = local.proxy_url
  }

  service_environment = {
    frontend = {
      INTERNAL_API_URL  = local.internal_api_url
      BETTER_AUTH_URL   = "https://${var.frontend_domain}"
      AUTH_DATABASE_URL = "postgresql://vector_auth@${local.db_endpoint}/${aws_db_instance.this.db_name}?search_path=auth&sslmode=require"
      REDIS_URL_RL      = local.rate_limit_redis_url
      # rate-limit ノードの署名用 cache 名 (common の REDIS_IAM_CACHE_NAME は broker 用)。
      REDIS_IAM_CACHE_NAME_RL = aws_elasticache_replication_group.rate_limit.replication_group_id
      # コードの既定値 (60/300) では通常の閲覧で session の枠が 429 になるため引き上げる。
      RATE_LIMIT_SESSION_PER_MIN = "600"
      RATE_LIMIT_IP_PER_MIN      = "3000"
      # 信頼できる client IP は ALB が XFF 末尾に追記した値だけ。aws_lb の xff_header_processing_mode = "append" と対で配る。
      CLIENT_IP_TRUST = "alb-xff-last"
    }
    api = {
      DATABASE_URL = local.backend_db_url["vector_api"]
      REDIS_URL    = local.broker_redis_url["api"]
      # 事前チェックが存在を見るだけで、呼び出しは agent が行う (api に IAM 権限も PrivateLink も与えない)。
      AGENTCORE_GATEWAY_URL = aws_bedrockagentcore_gateway.web_search.gateway_url
    }
    # scheduler は DB に接続しないが、Settings が database_url を必須とするため値だけ渡す (task role に rds-db:connect は無い)。
    scheduler = {
      DATABASE_URL = local.backend_db_url["vector_app"]
      REDIS_URL    = local.broker_redis_url["scheduler"]
    }
    insights = {
      DATABASE_URL = local.backend_db_url["vector_insights"]
      REDIS_URL    = local.broker_redis_url["insights"]
    }
    agent = {
      DATABASE_URL = local.backend_db_url["vector_agent"]
      REDIS_URL    = local.broker_redis_url["agent"]
      # 外部検索の入口 (agent.tf)。PrivateLink 経由の内部宛先なので proxy を通さない。
      AGENTCORE_GATEWAY_URL = aws_bedrockagentcore_gateway.web_search.gateway_url
    }
  }

  # サービス -> そのサービスが Connect してよい [cache ARN, user ARN]。
  # broker に繋ぐサービスは自分の user のみ、frontend は rate-limit ノードのみ。
  valkey_connect_arns = merge(
    {
      for s in local.broker_services : s => [
        aws_elasticache_replication_group.broker.arn,
        aws_elasticache_user.broker[s].arn,
      ]
    },
    {
      frontend = [
        aws_elasticache_replication_group.rate_limit.arn,
        aws_elasticache_user.frontend.arn,
      ]
    },
  )
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.services

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  task_role_arn      = aws_iam_role.task[each.key].arn
  execution_role_arn = aws_iam_role.execution[each.key].arn

  # 空配列は ECS で「CMD を空で上書き」になりコンテナが即死するため、command の無いサービスは key ごと省く。
  container_definitions = jsonencode([
    merge(
      length(each.value.command) == 0 ? {} : { command = each.value.command },
      {
        name      = each.key
        image     = "${aws_ecr_repository.this[each.value.image].repository_url}:${var.image_tag}"
        essential = true

        portMappings = each.value.port == null ? [] : [
          { containerPort = each.value.port, protocol = "tcp" }
        ]

        environment = [
          for k, v in merge(local.common_environment, local.service_environment[each.key]) :
          { name = k, value = v }
        ]

        # 起動前に execution role が取得する。値は Terraform の管理外。
        secrets = [
          for env_name, param in each.value.secrets : {
            name      = env_name
            valueFrom = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/${each.key}/${param}"
          }
        ]

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            # awslogs-create-group は使わない。log group は Terraform が作り、boundary は CreateLogGroup を許さない。
            "awslogs-group"         = aws_cloudwatch_log_group.this[each.key].name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = "ecs"
          }
        }
      },
    )
  ])
}

resource "aws_ecs_service" "this" {
  for_each = local.services

  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [aws_subnet.app[each.key].id]
    security_groups  = [aws_security_group.app[each.key].id]
    assign_public_ip = false
  }

  # singleton のサービスは新旧を並走させない (入れ替え中は止まる)。
  deployment_maximum_percent         = each.value.singleton ? 100 : 200
  deployment_minimum_healthy_percent = each.value.singleton ? 0 : 100

  dynamic "load_balancer" {
    for_each = each.key == "frontend" ? [1] : []

    content {
      target_group_arn = aws_lb_target_group.frontend.arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  # LB 付きのサービスだけに効く設定なので frontend にのみ余裕を持たせる。
  health_check_grace_period_seconds = each.key == "frontend" ? 120 : null

  dynamic "service_registries" {
    for_each = contains(local.discoverable_services, each.key) ? [1] : []

    content {
      registry_arn = aws_service_discovery_service.this[each.key].arn
    }
  }

  # image tag は rollout job が更新する。Terraform が巻き戻さない。
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.https]
}

# 内部の名前解決。到達の可否は security group だけが決める。
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = var.internal_namespace
  description = "Internal service discovery for Vector"
  vpc         = aws_vpc.main.id
}

# ここで登録される名前を起動時ガードが接尾辞で受理する。
#   backend  config.py の _enforce_internal_namespace_in_production
#   frontend lib/api/internal-config.ts
resource "aws_service_discovery_service" "this" {
  for_each = local.discoverable_services

  name = each.value

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  # failure_threshold は AWS 側で 1 固定だが、空ブロックだと毎回の plan が replace を要求するため明示する。
  health_check_custom_config {
    failure_threshold = 1
  }
}

# task role と execution role をサービスごとに分ける。secret を注入するのは execution role なので、
# 共有すると全サービスの secret を読める role ができる。

# confused deputy 対策で呼び出し元のアカウントと ECS を条件にする。
# task ARN は起動のたびに変わるため、SourceArn は region と account までに留める。
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
resource "aws_iam_role" "task" {
  for_each = local.services

  name               = "${var.name_prefix}-${each.key}-task"
  path               = "/${var.name_prefix}/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json

  # agent サービスだけ天井が違う。この task role だけが web search の gateway を呼ぶ
  # (agent.tf の aws_iam_role_policy.agentcore_gateway_invoke)。
  permissions_boundary = local.boundary_arns[each.key == "agent" ? "agent-task" : "task"]
}

resource "aws_iam_role_policy" "task" {
  for_each = local.db_services

  name = "rds-iam-auth"
  role = aws_iam_role.task[each.value].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RdsIamAuth"
        Effect = "Allow"
        Action = "rds-db:connect"
        # 入った後の権限は migration の GRANT が決める。DbiResourceId はスナップショット復元で変わるので resource_id を参照する。
        Resource = [
          for user in local.services[each.value].db_users :
          "arn:aws:rds-db:${var.region}:${local.account_id}:dbuser:${aws_db_instance.this.resource_id}/${user}"
        ]
      },
    ]
  })
}

# Connect は cache と user の両方の ARN で評価されるので両方並べる。入った後の権限は platform_valkey.tf の access string が決める。
resource "aws_iam_role_policy" "valkey" {
  for_each = local.valkey_connect_arns

  name = "elasticache-iam-auth"
  role = aws_iam_role.task[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ElastiCacheIamAuth"
        Effect   = "Allow"
        Action   = "elasticache:Connect"
        Resource = each.value
      },
    ]
  })
}

# --- execution role -------------------------------------------------------

resource "aws_iam_role" "execution" {
  for_each = local.services

  name                 = "${var.name_prefix}-${each.key}-exec"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = local.boundary_arns["execution"]
}

resource "aws_iam_role_policy" "execution" {
  for_each = local.services

  name = "ecs-task-execution"
  role = aws_iam_role.execution[each.key].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # 認証トークンは resource を絞れない。pull できる repo は下の EcrPull が決める。
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
      # log group の ARN は :* 付きと無しの両方を並べる。
      # CreateLogStream が接尾辞なしで評価される例があり、欠けると task が起動しない。
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
      # secret は Terraform で管理しない (value が refresh のたびに state に載るため)。サービスの分離は読める path で表す。
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

resource "aws_security_group" "app" {
  for_each = local.services

  name        = "${var.name_prefix}-app-${each.key}"
  description = "ECS task: ${each.key}"
  vpc_id      = aws_vpc.main.id
}

# --- データストア ---------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "rds_from_app" {
  for_each = local.db_services

  security_group_id            = aws_security_group.rds.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_rds" {
  for_each = local.db_services

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "RDS PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

resource "aws_vpc_security_group_ingress_rule" "valkey_broker_from_app" {
  for_each = local.broker_services

  security_group_id            = aws_security_group.valkey_broker.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_valkey_broker" {
  for_each = local.broker_services

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "Valkey broker"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.valkey_broker.id
}

# --- egress proxy ---------------------------------------------------------
#
# frontend は外への出先を持たないので含めない。

resource "aws_vpc_security_group_ingress_rule" "proxy_from_app" {
  for_each = local.egress_services

  security_group_id            = aws_security_group.proxy.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_proxy" {
  for_each = local.egress_services

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "egress proxy"
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
  referenced_security_group_id = aws_security_group.proxy.id
}

# --- VPC endpoint ---------------------------------------------------------
#
# endpoint の ENI は api の subnet にしか無いが、VPC 内の経路で全 subnet から届く。

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_app" {
  for_each = local.all_services

  security_group_id            = aws_security_group.endpoints.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_endpoints" {
  for_each = local.all_services

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "ECR / SSM / CloudWatch Logs endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.endpoints.id
}

# Fargate は ECR のレイヤーを S3 から直接取るので、全サービスに S3 への 443 が要る。
# 取得できる対象は aws_vpc_endpoint.s3 の endpoint policy が縛る。
resource "aws_vpc_security_group_egress_rule" "app_to_s3" {
  for_each = local.all_services

  security_group_id = aws_security_group.app[each.value].id
  description       = "ECR image layers via S3 Gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
}

resource "aws_cloudwatch_log_group" "this" {
  for_each = local.services

  name              = "/ecs/${var.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.name_prefix}-${each.key}" }
}
