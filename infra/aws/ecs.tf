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

  # IAM DB auth の可視的な帰結: **URL から password の項が消える**。
  # 消えたので URL 自体が秘密でなくなり、SSM ではなく environment に置ける。
  #
  # 規準: **URL に secret が含まれるなら URL ごと SSM、含まれないなら env。**
  # DATABASE は password が消えたので env、Valkey は RBAC password が残る
  # (IAM 認証を見送った) ので REDIS_URL ごと SSM。
  # 帰結として Valkey の endpoint が変わると SSM の値を手で更新する必要がある
  # (Terraform は endpoint を知っているのに配れない)。runbook に載せる。
  #
  # `sslmode=require` は db_ssl.py が verify-full に格上げする。
  backend_db_url = {
    for user in toset(["vector_app", "vector_collect", "vector_auth"]) :
    user => "postgresql+asyncpg://${user}@${local.db_endpoint}/${aws_db_instance.this.db_name}?sslmode=require"
  }

  common_environment = {
    ENV        = "production"
    AWS_REGION = var.region
    # IAM モードは明示フラグで入る。「password が無いから IAM」という推測にすると、
    # password の設定漏れが黙って IAM モードとして動いてしまう。
    DB_IAM_AUTH                = "true"
    INTERNAL_FRONTEND_BASE_URL = local.internal_frontend_url
    # proxy を通さない宛先。ECS の credential endpoint を入れないと SDK の
    # 資格情報取得が proxy に迂回して死ぬ。内部 DNS を入れないと revalidate と
    # BFF が proxy の private 宛先拒否で静かに失敗する。
    # RDS は 5432 の TCP で HTTP クライアントを通らないので入れない。
    NO_PROXY = join(",", [
      "169.254.169.254",
      "169.254.170.2",
      ".${var.internal_namespace}",
    ])
    # SDK 経路 (DeepSeek / Gemini / Logfire) はこの env var を拾う。
    # `make_safe_async_client` を通る経路 (Tavily / RSS) は明示 transport を
    # 渡すため httpx が env proxy を無視するので、settings 経由で proxy= に
    # 注入する必要がある。**渡し方が 2 系統ある。**
    HTTPS_PROXY = local.proxy_url
    HTTP_PROXY  = local.proxy_url
    # 2 系統のうち settings 側。config.py の egress_proxy_url がこれを受け、
    # make_safe_async_client が全 client に proxy として差し込む。
    #
    # 上の NO_PROXY はこちらには効かない (env を読まない経路なので)。factory の
    # 呼び出し先が RSS / スクレイプ / Tavily と全て外部宛先で、内部宛先を叩く
    # revalidate は raw httpx 側に居るから成立している。内部宛先を factory 経由で
    # 呼ぶ経路を作ると、proxy の private 宛先拒否で静かに失敗する。
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
      INTERNAL_API_URL  = local.internal_api_url
      BETTER_AUTH_URL   = "https://${var.frontend_domain}"
      AUTH_DATABASE_URL = "postgresql://vector_auth@${local.db_endpoint}/${aws_db_instance.this.db_name}?search_path=auth&sslmode=require"
    }
    api = {
      FRONTEND_URL = "https://${var.frontend_domain}"
      DATABASE_URL = local.backend_db_url["vector_app"]
    }
    # scheduler は engine を作らないが、config.py の `database_url: str` が
    # 必須設定なので値が無いと Settings の構築で落ちる。
    # ただし task role に rds-db:connect は無いので、URL があっても接続はできない。
    # **IAM の境界が設定の契約より狭い**状態で、権限としては正しい。
    scheduler = {
      DATABASE_URL = local.backend_db_url["vector_app"]
    }
    fetch = {
      DATABASE_URL = local.backend_db_url["vector_collect"]
    }
    analysis = {
      DATABASE_URL                = local.backend_db_url["vector_app"]
      AUTH_RETENTION_DATABASE_URL = local.backend_db_url["vector_auth"]
      # 本番では明示的に有効化する (config.py の default は両方 false)。
      BACKFILL_ASSESSMENTS_ENABLED = "true"
      BACKFILL_EMBEDDINGS_ENABLED  = "true"
    }
    insights = {
      DATABASE_URL = local.backend_db_url["vector_app"]
    }
    agent = {
      DATABASE_URL = local.backend_db_url["vector_app"]
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

  # image tag は app-deploy workflow が更新する。Terraform が巻き戻さない。
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.https]
}
