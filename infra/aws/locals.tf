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
      image          = "backend", db_users = ["vector_app"]
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
      image          = "backend", db_users = ["vector_app"]
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

  # image は 2 つ (backend / frontend) を段で共有する。
  # proxy の image もここで作る (repo は image の関心事であって段の関心事ではない)。
  images = toset(concat([for _, s in local.stages : s.image], ["proxy"]))

  # 内部から名前で呼ばれる段だけ Cloud Map に登録する。
  # frontend → api (BFF) と insights → frontend (revalidate) の 2 経路だけ。
  discoverable_stages = toset(["frontend", "api"])

  proxy_url             = "http://proxy.${var.internal_namespace}:${var.proxy_port}"
  internal_api_url      = "http://api.${var.internal_namespace}:8000/api/v1"
  internal_frontend_url = "http://frontend.${var.internal_namespace}:3000"

  # NO_PROXY へ入れる AgentCore Gateway の host。gateway_url は
  # https://<host>/<path> 形式で apply 時に確定する。suffix を literal で書くと
  # 命名規則が変わったときに proxy へ迂回して静かに失敗するため、URL から取る。
  agentcore_gateway_host = regex(
    "^https?://([^/]+)", aws_bedrockagentcore_gateway.web_search.gateway_url
  )[0]
}
