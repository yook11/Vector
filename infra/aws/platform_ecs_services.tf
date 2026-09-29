# 段の宣言。AWS-MIGRATION-INVENTORY.local.md の棚卸しがそのまま入る。
# subnet / security group / (後続の) IAM role・Valkey user は、この 1 表から生成する。
# 段を増やすときに触るのはここだけになる。
locals {
  # subnet_index は VPC CIDR からの切り出し位置。並べ替えで CIDR がずれないよう
  # 明示する (map のキー順に依存させない)。
  #
  # 各列は「その段が実際に使うもの」であって「使いそうなもの」ではない。
  #
  # 例外: secrets の bff-jwt-signing-secret / revalidate-bearer-secret は backend
  # 全段に配る。実際に使うのは api / insights だが、Settings が構築時に必須と
  # する契約のため (scheduler の DATABASE_URL と同じ「設定の契約が実使用より
  # 広い」枠)。
  #
  # - db_users: IAM DB auth で名乗れる Postgres ロール。**scheduler は空**。
  #   cron を発火するだけで DB engine を作らない (scheduler_entrypoint.py が
  #   is_scheduler_process=True で WORKER_STARTUP を立てず、lifecycle.py の
  #   engine 生成 hook が走らない)。
  # - image: backend は 1 つの image を 4 段が command 違いで起動する。
  #   frontend だけ別 image。
  # - needs_egress: frontend は外部への出先を持たない (Logfire も外部 API も無い)。
  stages = {
    frontend = {
      subnet_index   = 20, needs_broker = false
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
      subnet_index   = 21, needs_broker = true
      egress_vendors = ["logfire"], egress_allow_any_domain = false
      image          = "backend", db_users = ["vector_api"]
      cpu            = 256, memory = 512, port = 8000, singleton = false
      command        = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
      secrets = {
        # research 開始 API の設定プリフライトが両 key の存在を要求する。
        # api は presence check のみで実呼び出しは agent 段が担うため egress は不要。
        DEEPSEEK_API_KEY         = "deepseek-api-key"
        TAVILY_API_KEY           = "tavily-api-key"
        BFF_JWT_SIGNING_SECRET   = "bff-jwt-signing-secret"
        REVALIDATE_BEARER_SECRET = "revalidate-bearer-secret"
        LOGFIRE_TOKEN            = "logfire-token"
      }
    }
    # singleton: 新旧が並走すると cron が二重発火するので、deployment configuration で
    # 1 task に保つ。
    scheduler = {
      subnet_index   = 22, needs_broker = true
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
      subnet_index   = 25, needs_broker = true
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
      subnet_index   = 26, needs_broker = true
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

  # subnet の CIDR はここで確定させる。resource の cidr_block から読み戻すと
  # plan 時に unknown になり、squid.conf の差分が読めなくなる。
  app_subnet_cidrs = { for name, s in local.stages : name => cidrsubnet(var.vpc_cidr, 8, s.subnet_index) }

  all_stages    = toset(keys(local.stages))
  db_stages     = toset([for name, s in local.stages : name if length(s.db_users) > 0])
  broker_stages = toset([for name, s in local.stages : name if s.needs_broker])
  egress_stages = toset([
    for name, s in local.stages : name
    if length(s.egress_vendors) > 0 || s.egress_allow_any_domain
  ])

  # 内部から名前で呼ばれる段だけ Cloud Map に登録する。
  # frontend → api (BFF) と insights → frontend (revalidate) の 2 経路だけ。
  discoverable_stages   = toset(["frontend", "api"])
  internal_api_url      = "http://api.${var.internal_namespace}:8000/api/v1"
  internal_frontend_url = "http://frontend.${var.internal_namespace}:3000"
}

resource "aws_ecs_cluster" "this" {
  name = var.name_prefix

  # Container Insights は有効にすると CloudWatch のカスタムメトリクス課金が乗る。
  # Logfire で可観測性を賄っているので二重に払わない。
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

locals {
  common_environment = {
    ENV        = "production"
    AWS_REGION = var.region
    # Settings が構築時に全段で要求する必須項目 (実際に使うのは frontend_url が
    # api、crossref は取得の Lambda だけ)。frontend (Node) は読まないが common で害はない。
    FRONTEND_URL           = "https://${var.frontend_domain}"
    CROSSREF_CONTACT_EMAIL = var.crossref_contact_email
    # IAM モードは明示フラグで入る。「password が無いから IAM」という推測にすると、
    # password の設定漏れが黙って IAM モードとして動いてしまう。
    DB_IAM_AUTH    = "true"
    REDIS_IAM_AUTH = "true"
    # token 署名の host は DNS endpoint ではなく cache 名 (URL から導出できない)。
    # 値は broker ノードの名前なので、rate-limit ノードに繋ぐ側はこれを読まない。
    REDIS_IAM_CACHE_NAME       = aws_elasticache_replication_group.broker.replication_group_id
    INTERNAL_FRONTEND_BASE_URL = local.internal_frontend_url
    # proxy を通さない宛先。ECS の credential endpoint を入れないと SDK の
    # 資格情報取得が proxy に迂回して死ぬ。内部 DNS と gateway host は env を読む
    # client のための保険で、backend の内部宛 client (make_internal_async_client)
    # は env を読まないので依存しない。
    # RDS は 5432 の TCP で HTTP クライアントを通らないので入れない。
    NO_PROXY = join(",", [
      "169.254.169.254",
      "169.254.170.2",
      ".${var.internal_namespace}",
      # AgentCore Gateway は PrivateLink 経由の内部宛先。host は gateway_url
      # から取り、suffix を推測しない。
      local.agentcore_gateway_host,
    ])
    # SDK 経路 (DeepSeek / Gemini / Logfire) はこの env var を拾う。backend の
    # HTTP client は拾わない: 第三者宛の `make_external_async_client` は明示
    # transport を渡すため httpx が env proxy を無視し (settings 経由で注入する)、
    # 内部宛の `make_internal_async_client` はそもそも env を読まない。
    # **経路の決まり方が 3 通りある。**
    #
    # common なので frontend にも入るが、frontend は proxy への SG egress を持たない。
    # Node は既定でこの env を読まないため現状は不活性で、読むライブラリが入ると
    # frontend だけ到達不能で詰まる。その時は stage_environment 側へ移す。
    HTTPS_PROXY = local.proxy_url
    HTTP_PROXY  = local.proxy_url
    # 3 通りのうち settings 側。config.py の egress_proxy_url がこれを受け、
    # make_external_async_client が第三者宛の全 client に proxy として差し込む。
    #
    # 上の NO_PROXY はこちらには効かない (env を読まない経路なので)。内部宛先は
    # make_internal_async_client 側に分かれていて proxy を経由しないため、宛先の
    # 分類さえ守れば private 宛先拒否を踏まない。
    #
    # common に置くので frontend にも入るが、frontend は Node の image で
    # この値を読まない (Python の Settings field)。
    EGRESS_PROXY_URL = local.proxy_url
  }

  # 段ごとの追加 env。
  #
  stage_environment = {
    frontend = {
      INTERNAL_API_URL  = local.internal_api_url
      BETTER_AUTH_URL   = "https://${var.frontend_domain}"
      AUTH_DATABASE_URL = "postgresql://vector_auth@${local.db_endpoint}/${aws_db_instance.this.db_name}?search_path=auth&sslmode=require"
      REDIS_URL_RL      = local.rate_limit_redis_url
      # rate-limit ノード用の署名 host (common の REDIS_IAM_CACHE_NAME は broker の
      # 名前なので frontend はそちらを読まない)。
      REDIS_IAM_CACHE_NAME_RL = aws_elasticache_replication_group.rate_limit.replication_group_id
      # コード default (60/300) は通常閲覧で session bucket が 429 になる実測済み。
      RATE_LIMIT_SESSION_PER_MIN = "600"
      RATE_LIMIT_IP_PER_MIN      = "3000"
      # 入口が ALB なので、信頼できる client IP は ALB が XFF 末尾へ追記した値だけ。
      # 未宣言だと per-IP 制限と Better Auth の login limiter が共有バケツに退化するため、
      # aws_lb の xff_header_processing_mode = "append" と対で必ず配る。
      CLIENT_IP_TRUST = "alb-xff-last"
    }
    api = {
      DATABASE_URL = local.backend_db_url["vector_api"]
      REDIS_URL    = local.broker_redis_url["api"]
      # research 開始 API の設定プリフライトが presence を見るだけ。実呼び出しは
      # agent 段が担うので、api には IAM 権限も PrivateLink も与えない。
      AGENTCORE_GATEWAY_URL = aws_bedrockagentcore_gateway.web_search.gateway_url
    }
    # scheduler は engine を作らないが、config.py の `database_url: str` が
    # 必須設定なので値が無いと Settings の構築で落ちる。
    # ただし task role に rds-db:connect は無いので、URL があっても接続はできない。
    # **IAM の境界が設定の契約より狭い**状態で、権限としては正しい。
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
      # 外部検索の MCP 入口 (agentcore.tf)。宛先は PrivateLink 経由の内部 host で、
      # backend は make_internal_async_client で叩く (env の proxy 設定を読まない)。
      AGENTCORE_GATEWAY_URL = aws_bedrockagentcore_gateway.web_search.gateway_url
    }
  }

  # 段 -> その段が Connect してよい [cache ARN, user ARN]。
  # broker に繋ぐ段は自分の user のみ、frontend は rate-limit ノードのみ。
  valkey_connect_arns = merge(
    {
      for s in local.broker_stages : s => [
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
  for_each = local.stages

  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory

  # x86 比で約 20% 安い。依存の aarch64 wheel は 2026-07-27 に全数確認済み。
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  task_role_arn      = aws_iam_role.task[each.key].arn
  execution_role_arn = aws_iam_role.execution[each.key].arn

  # command は空配列を渡さない。ECS では「未指定」ではなく「CMD を空で上書き」に
  # なり得るため、image 側の CMD (frontend は `node server.js`、ENTRYPOINT 無し) が
  # 消えてコンテナが即死する。指定しない段では key ごと省く。
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
          for k, v in merge(local.common_environment, local.stage_environment[each.key]) :
          { name = k, value = v }
        ]

        # ECS が起動前に execution role で取得する。task role ではない。
        # 値は Terraform の管理外 (CLI で put-parameter する)。
        secrets = [
          for env_name, param in each.value.secrets : {
            name      = env_name
            valueFrom = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/${var.name_prefix}/${each.key}/${param}"
          }
        ]

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            # awslogs-create-group は使わない。log group は Terraform が作るので、
            # execution role に logs:CreateLogGroup が要らない (boundary とも整合)。
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
  for_each = local.stages

  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # 冗長化の水準は RDS Single-AZ が決めている。同じ AZ に置いて cross-AZ 転送費を
  # ゼロにする。public IP は付けない (外へ出る経路は egress proxy だけ)。
  network_configuration {
    subnets          = [aws_subnet.app[each.key].id]
    security_groups  = [aws_security_group.app[each.key].id]
    assign_public_ip = false
  }

  # singleton の段は新旧を並走させない。既定 (200/100) だと deploy 中に
  # scheduler が 2 つ動いて cron が二重発火する。
  # 対価は入れ替え中の停止で、worker と scheduler では許容できる。
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

  # LB 付きの段だけに効く設定なので frontend にのみ余裕を持たせる。
  health_check_grace_period_seconds = each.key == "frontend" ? 120 : null

  dynamic "service_registries" {
    for_each = contains(local.discoverable_stages, each.key) ? [1] : []

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

# 内部到達は Cloud Map の service discovery。
# ECS Service Connect は Envoy sidecar が各 task に載ってサイズ表を壊すので採らない。
# 到達制御は security group だけが行い、名前解決は権限に関与しない。
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = var.internal_namespace
  description = "Internal service discovery for Vector"
  vpc         = aws_vpc.main.id
}

# ここで登録される名前を起動時ガードが接尾辞で受理する。
#   backend  config.py の _enforce_internal_namespace_in_production
#   frontend lib/api/internal-config.ts
resource "aws_service_discovery_service" "this" {
  for_each = local.discoverable_stages

  name = each.value

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  # ECS が task の生死に応じて登録・解除する。Cloud Map 自身の health check は
  # 使わない (課金対象で、ECS の管理と二重になる)。
  # failure_threshold は AWS 側で 1 固定だが、空ブロックだと provider が API に
  # 送らず実体が null になり、毎 plan が replace を要求し続ける。固定値を明示して
  # round-trip を成立させる。
  health_check_custom_config {
    failure_threshold = 1
  }
}

# 段ごとに task role と execution role を 1 つずつ。計 14。
#
# なぜ execution role まで段ごとに分けるか: ECS の secret 注入は
# コンテナ起動前に **execution role** が行う (task role ではない)。共有すると
# 「全段の secret を読める role が 1 つ存在する」状態になり、段の分離がそこで崩れる。

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
# ログは Logfire、ストレージ無し。よって task role に載るのは IAM auth の入口
# 2 アクション (rds-db:connect / elasticache:Connect) だけになる。
#
# これは偶然ではなくキュー選定の帰結。SQS を採っていれば段ごとに
# sqs:SendMessage / ReceiveMessage / DeleteMessage が載り、IAM が主戦場のままだった。
# Valkey を選んだ時点で、権限設計の重心が IAM から Redis ACL と Postgres の GRANT へ移った。
resource "aws_iam_role" "task" {
  for_each = local.stages

  name               = "${var.name_prefix}-${each.key}-task"
  path               = "/${var.name_prefix}/"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json

  # agent 段だけ天井が違う。この task role だけが web search の gateway を呼ぶ
  # (agentcore.tf の aws_iam_role_policy.agentcore_gateway_invoke)。
  permissions_boundary = local.boundary_arns[each.key == "agent" ? "agent-task" : "task"]
}

# scheduler には DB の policy を付けない。cron を発火するだけで DB engine を
# 作らないため、持つのは下の elasticache:Connect だけになる。
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

# rds-db:connect と同型の入口権限。Connect は接続先 cache と接続 user の
# **両方の ARN** に対して評価されるため、片方だけでは認証が通らない。
# 入った後に何ができるかは valkey.tf の access_string が全部決める。
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
  for_each = local.stages

  name                 = "${var.name_prefix}-${each.key}-exec"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = local.boundary_arns["execution"]
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

resource "aws_security_group" "app" {
  for_each = local.stages

  name        = "${var.name_prefix}-app-${each.key}"
  description = "ECS task: ${each.key}"
  vpc_id      = aws_vpc.main.id
}

# --- データストア ---------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "rds_from_app" {
  for_each = local.db_stages

  security_group_id            = aws_security_group.rds.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_rds" {
  for_each = local.db_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "RDS PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.rds.id
}

# frontend は broker 側の Valkey に繋がない。rate-limit ノードは eviction 方針が
# 真逆 (volatile-ttl / noeviction) で、parameter group がノード単位なので別ノード。
resource "aws_vpc_security_group_ingress_rule" "valkey_broker_from_app" {
  for_each = local.broker_stages

  security_group_id            = aws_security_group.valkey_broker.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_valkey_broker" {
  for_each = local.broker_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "Valkey broker"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.valkey_broker.id
}

# --- egress proxy ---------------------------------------------------------
#
# frontend は含めない。Logfire も外部 API も持たないため、外に出る手段を
# 1 つも与えない。入口を持つ唯一の段が egress ゼロになる。

resource "aws_vpc_security_group_ingress_rule" "proxy_from_app" {
  for_each = local.egress_stages

  security_group_id            = aws_security_group.proxy.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_proxy" {
  for_each = local.egress_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "egress proxy"
  ip_protocol                  = "tcp"
  from_port                    = var.proxy_port
  to_port                      = var.proxy_port
  referenced_security_group_id = aws_security_group.proxy.id
}

# --- VPC endpoint ---------------------------------------------------------
#
# ENI は api の subnet 1 つだけに置くが、到達は VPC の local ルートで全 subnet
# から届く。ここを開けないと image pull も secret 注入も log 送信も落ちる。

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_app" {
  for_each = local.all_stages

  security_group_id            = aws_security_group.endpoints.id
  description                  = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.app[each.value].id
}

resource "aws_vpc_security_group_egress_rule" "app_to_endpoints" {
  for_each = local.all_stages

  security_group_id            = aws_security_group.app[each.value].id
  description                  = "ECR / SSM / CloudWatch Logs endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.endpoints.id
}

# ECR のレイヤー実体は S3 にあり、Fargate は task ENI から S3 へ直接取りに行く。
# ルートテーブルに Gateway endpoint の経路があっても、SG の egress が無ければ
# ここで落ちて task が起動しない (「規則ゼロは全拒否」がそのまま効く)。
# 宛先は gateway endpoint が export する prefix list で参照する。
#
# frontend にもこの穴が要る = 「frontend は外に出られない」が形式上は崩れる。
# 何を取れるかは aws_vpc_endpoint.s3 の endpoint policy が縛る (endpoints.tf)。
resource "aws_vpc_security_group_egress_rule" "app_to_s3" {
  for_each = local.all_stages

  security_group_id = aws_security_group.app[each.value].id
  description       = "ECR image layers via S3 Gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
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
