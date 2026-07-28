# egress proxy。NAT Gateway の代わりに、VPC の外へ出る唯一の経路になる。
#
# **stages の表には入れない。** proxy は「アプリの段」ではなく「経路の装置」で、
# public subnet に置き / 別 SG を持ち / allowlist の対象ではなく適用する側で /
# DB にも Valkey にも繋がない。表に入れると全列に例外が入り、表の意味が薄まる。

locals {
  # 宛先はコードから抽出した実ホスト。
  #   api.deepseek.com / api.tavily.com   app 内の URL リテラル
  #   generativelanguage.googleapis.com   google-genai SDK (Vertex ではない方)
  #   logfire-{us,eu}.pydantic.dev        logfire SDK の REGIONS[*].base_url
  #
  # Logfire は最初 .info と logfire.pydantic.dev を並べていたが、実 Squid で
  # 名前解決に失敗して発覚した。**.info は staging、logfire.pydantic.dev は
  # ダッシュボード**で、送信先ではない。正しい値は logfire/_internal/auth.py の
  # REGIONS。token の region で us / eu が決まるので両方入れる。
  egress_vendor_domains = {
    logfire  = ["logfire-us.pydantic.dev", "logfire-eu.pydantic.dev"]
    gemini   = ["generativelanguage.googleapis.com"]
    deepseek = ["api.deepseek.com"]
    tavily   = ["api.tavily.com"]
  }

  proxy_clients = {
    for name in local.egress_stages : name => {
      cidr = local.app_subnet_cidrs[name]
      domains = flatten([
        for vendor in local.stages[name].egress_vendors :
        local.egress_vendor_domains[vendor]
      ])
      unrestricted = local.stages[name].egress_unrestricted
    }
  }

  # 非公開レンジの正本は **app 側のファイル 1 つ**。Squid はレンジの明示列挙しか
  # 書けないので、ここから読んで conf を生成する。
  # 一致は backend の TestNonPublicRangeParity が固定する
  # (不変条件: proxy が拒否するものは app も必ず拒否する)。
  non_public_ranges = jsondecode(
    file("${path.module}/../../backend/app/shared/security/non_public_ranges.json")
  )

  squid_conf = templatefile("${path.module}/templates/squid.conf.tftpl", {
    listen_port       = var.proxy_port
    clients           = local.proxy_clients
    private_v4_ranges = local.non_public_ranges.v4
    private_v6_ranges = local.non_public_ranges.v6
  })
}

resource "aws_cloudwatch_log_group" "proxy" {
  name              = "/ecs/${var.name_prefix}/proxy"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.name_prefix}-proxy" }
}

# proxy も AWS の API を呼ばない。設定は env で渡すので SSM も要らず、
# **task role は空**になる (scheduler と同じ形)。
resource "aws_iam_role" "proxy_task" {
  name                 = "${var.name_prefix}-proxy-task"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_iam_role" "proxy_exec" {
  name                 = "${var.name_prefix}-proxy-exec"
  path                 = "/${var.name_prefix}/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_tasks_trust.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_iam_role_policy" "proxy_exec" {
  name = "ecs-task-execution"
  role = aws_iam_role.proxy_exec.id

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
        Resource = aws_ecr_repository.this["proxy"].arn
      },
      {
        Sid    = "CloudWatchLogsWrite"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          aws_cloudwatch_log_group.proxy.arn,
          "${aws_cloudwatch_log_group.proxy.arn}:*",
        ]
      },
    ]
  })
}

# 内部から名前で呼ばれる 3 つ目。Fargate は task IP が動的なので、
# HTTPS_PROXY に書ける固定の宛先が要る。内部 NLB は月 $16 かかる上に
# proxy が 1 台である以上 SPOF は減らないので採らない。
resource "aws_service_discovery_service" "proxy" {
  name = "proxy"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {}
}

resource "aws_ecs_task_definition" "proxy" {
  family                   = "${var.name_prefix}-proxy"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  task_role_arn      = aws_iam_role.proxy_task.arn
  execution_role_arn = aws_iam_role.proxy_exec.arn

  container_definitions = jsonencode([
    {
      name      = "proxy"
      image     = "${aws_ecr_repository.this["proxy"].repository_url}:${var.proxy_image_tag}"
      essential = true

      portMappings = [{ containerPort = var.proxy_port, protocol = "tcp" }]

      # 設定は env で丸ごと渡す。秘密を含まないので SSM を経由する必要がなく、
      # plan の差分で allowlist の変更がそのまま読める。
      environment = [
        { name = "SQUID_CONF", value = local.squid_conf },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.proxy.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "proxy" {
  name            = "proxy"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.proxy.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # 自前 task に public IP は与えない。外へは rt-proxy → NAT → IGW で出る。
  # インターネットからこの task を宛先に指定する手段が存在しない状態にする。
  network_configuration {
    subnets          = [aws_subnet.proxy.id]
    security_groups  = [aws_security_group.proxy.id]
    assign_public_ip = false
  }

  # 並走して困らないので新旧を重ねて切れ目をゼロにする
  # (scheduler と違い、proxy は 2 つ動いても副作用が無い)。
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  service_registries {
    registry_arn = aws_service_discovery_service.proxy.arn
  }

  # app 段と違い ignore_changes を付けない。**allowlist は task definition の env に
  # 焼かれている**ので、ignore すると「plan では差分が読めるのに service には
  # 配送されない」状態になる。app 段でこのパターンが成立するのは app-deploy が
  # revision を進めるからで、proxy は app-deploy の対象外 (backend / frontend のみ)。
  # proxy の task definition を触るのは Terraform だけなので、理由が当てはまらない。
}
