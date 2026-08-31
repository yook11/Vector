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
  db_endpoint = "${aws_db_instance.this.address}:${aws_db_instance.this.port}"

  # IAM auth の可視的な帰結: **URL から password の項が消える**。
  # 消えたので URL 自体が秘密でなくなり、SSM ではなく environment に置ける。
  #
  # 規準: **URL に secret が含まれるなら URL ごと SSM、含まれないなら env。**
  # DATABASE も Valkey も IAM 認証で password が消えたので env。endpoint は
  # Terraform が知っているので、変わっても URL が追従する (手動更新の余地が無い)。
  #
  # `sslmode=require` は db_ssl.py が verify-full に格上げする。
  backend_db_url = {
    for user in toset(["vector_app", "vector_collect", "vector_auth"]) :
    user => "postgresql+asyncpg://${user}@${local.db_endpoint}/${aws_db_instance.this.db_name}?sslmode=require"
  }

  broker_endpoint     = "${aws_elasticache_replication_group.broker.primary_endpoint_address}:${aws_elasticache_replication_group.broker.port}"
  rate_limit_endpoint = "${aws_elasticache_replication_group.rate_limit.primary_endpoint_address}:${aws_elasticache_replication_group.rate_limit.port}"

  # rediss は transit_encryption_enabled = true の帰結。username が IAM user を
  # 名指しし、token は app が接続ごとに SigV4 署名で生成するので password 項は無い。
  broker_redis_url = {
    for s in local.broker_stages :
    s => "rediss://${var.name_prefix}-${s}@${local.broker_endpoint}/0"
  }
  rate_limit_redis_url = "rediss://${var.name_prefix}-frontend@${local.rate_limit_endpoint}/0"

  common_environment = {
    ENV        = "production"
    AWS_REGION = var.region
    # Settings が構築時に全段で要求する必須項目 (実際に使うのは frontend_url が
    # api、crossref が fetch)。frontend (Node) は読まないが common で害はない。
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
  # analysis だけ DB URL を 2 本持つ。同居する maintenance worker の
  # purge_auth_rate_limits が auth."rateLimit" を消すため、
  # lifecycle.py の build_auth_retention_engine が別 engine を建てる。
  # 「1 task = 1 DB user」を仮定した形では表現できない。
  stage_environment = {
    frontend = {
      # RDS の CA は Node 内蔵 store に無い private root。pg は内蔵 store を使うので
      # ここで足す (追加であって置換ではない)。path は Dockerfile の COPY 先。
      NODE_EXTRA_CA_CERTS = "/app/rds-ca-ap-northeast-1.pem"
      INTERNAL_API_URL    = local.internal_api_url
      BETTER_AUTH_URL     = "https://${var.frontend_domain}"
      AUTH_DATABASE_URL   = "postgresql://vector_auth@${local.db_endpoint}/${aws_db_instance.this.db_name}?search_path=auth&sslmode=require"
      REDIS_URL_RL        = local.rate_limit_redis_url
      # rate-limit ノード用の署名 host (common の REDIS_IAM_CACHE_NAME は broker の
      # 名前なので frontend はそちらを読まない)。
      REDIS_IAM_CACHE_NAME_RL = aws_elasticache_replication_group.rate_limit.replication_group_id
      # コード default (60/300) は通常閲覧で session bucket が 429 になる実測済み。
      # Fly secrets の運用値と同値に揃える。
      RATE_LIMIT_SESSION_PER_MIN = "600"
      RATE_LIMIT_IP_PER_MIN      = "3000"
      # 入口が ALB なので、信頼できる client IP は ALB が XFF 末尾へ追記した値だけ。
      # 未宣言だと per-IP 制限と Better Auth の login limiter が共有バケツに退化するため、
      # aws_lb の xff_header_processing_mode = "append" と対で必ず配る。
      CLIENT_IP_TRUST = "alb-xff-last"
    }
    api = {
      DATABASE_URL = local.backend_db_url["vector_app"]
      REDIS_URL    = local.broker_redis_url["api"]
    }
    # scheduler は engine を作らないが、config.py の `database_url: str` が
    # 必須設定なので値が無いと Settings の構築で落ちる。
    # ただし task role に rds-db:connect は無いので、URL があっても接続はできない。
    # **IAM の境界が設定の契約より狭い**状態で、権限としては正しい。
    scheduler = {
      DATABASE_URL = local.backend_db_url["vector_app"]
      REDIS_URL    = local.broker_redis_url["scheduler"]
    }
    fetch = {
      DATABASE_URL = local.backend_db_url["vector_collect"]
      REDIS_URL    = local.broker_redis_url["fetch"]
    }
    analysis = {
      DATABASE_URL                = local.backend_db_url["vector_app"]
      AUTH_RETENTION_DATABASE_URL = local.backend_db_url["vector_auth"]
      REDIS_URL                   = local.broker_redis_url["analysis"]
      # 本番では明示的に有効化する (config.py の default は両方 false)。
      BACKFILL_ASSESSMENTS_ENABLED = "true"
      BACKFILL_EMBEDDINGS_ENABLED  = "true"
    }
    insights = {
      DATABASE_URL = local.backend_db_url["vector_app"]
      REDIS_URL    = local.broker_redis_url["insights"]
    }
    agent = {
      DATABASE_URL = local.backend_db_url["vector_app"]
      REDIS_URL    = local.broker_redis_url["agent"]
      # 外部検索の MCP 入口 (agentcore.tf)。外部検索を持つのは agent 段だけなので
      # common へは置かない。宛先は PrivateLink 経由の内部 host で、下の NO_PROXY が
      # proxy を迂回させる。
      AGENTCORE_GATEWAY_URL = aws_bedrockagentcore_gateway.web_search.gateway_url
    }
  }
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

  # worker-analysis は起動時の import が重く、0.25 vCPU では約 2 倍に伸びる。
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
