# ===========================================
# Vector — 運用ターゲット
# ===========================================
# 設計原則:
#   1. 動詞は最小限。覚えられない量は書かない
#   2. 単発コマンドは入れない（透明性が落ちる）
#   3. 状態を変える target は必ず末尾で検証する
# ===========================================

.DEFAULT_GOAL := help
.PHONY: help \
        pipeline-up pipeline-down pipeline-restart pipeline-status pipeline-logs \
        migrate-safe migrate-prod verify-env verify-config \
        test-integration test-integration-up test-integration-down \
        test-integration-guard test-integration-print-project

# サービス分類（変更する時はここだけ触る）
WORKERS  := worker-fetch worker-analysis worker-insights scheduler
PIPELINE := backend $(WORKERS)
QUEUES   := pipeline:metadata pipeline:content pipeline:analysis pipeline:embedding digest briefing

# 統合テスト専用 Postgres を立てる compose の呼び出し前置詞。
# project 名を worktree ごとに分離し、dev compose の project 名 (= worktree
# ディレクトリ basename) との衝突を `vector-test-` prefix で構造的に防ぐ。
# basename を compose project 名規則 [a-z0-9_-] に正規化し、同名 basename の
# 別 worktree を CURDIR の cksum hash で曖昧性解消する。空 / prefix 不一致の
# project 名は `down --remove-orphans` が dev stack を巻き添え削除するため、
# test-integration-guard で fail-closed にする。
TEST_PROJECT_SLUG := $(shell printf '%s' "$(notdir $(CURDIR))" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9]/-/g' -e 's/--*/-/g' -e 's/^-//' -e 's/-$$//')
TEST_PROJECT_HASH := $(shell printf '%s' "$(CURDIR)" | cksum | cut -d' ' -f1)
TEST_COMPOSE_PROJECT := vector-test-$(TEST_PROJECT_SLUG)-$(TEST_PROJECT_HASH)
# project 名は必ず quote する。unquoted だと command-line override
# (例: TEST_COMPOSE_PROJECT='vector-test-x -p vector') が docker compose の
# option 注入になり得るため。文字種検証は test-integration-guard が担う。
TEST_COMPOSE := docker compose -p "$(TEST_COMPOSE_PROJECT)" -f docker-compose.test.yml

help:  ## ターゲット一覧
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
	  | awk -F':.*##' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# -------------------------------------------
# パイプライン操作
# -------------------------------------------

pipeline-up: verify-env  ## 全サービスを起動して状態確認
	docker compose up -d
	@echo "→ healthy 待機中（最大 60 秒）..."
	@for i in 1 2 3 4 5 6; do \
	  sleep 10; \
	  unhealthy=$$(docker compose ps --format json \
	    | jq -r 'select(.Health != "" and .Health != "healthy") | .Name'); \
	  [ -z "$$unhealthy" ] && break; \
	done
	@$(MAKE) --no-print-directory pipeline-status

pipeline-restart: verify-env  ## 設定/ORM 変更後の再起動（env と ORM を確実に再読込）
	docker compose up -d --force-recreate $(PIPELINE)
	@sleep 15
	@$(MAKE) --no-print-directory pipeline-status

pipeline-down:  ## パイプライン停止（DB/Redis は残す → データ保持）
	docker compose stop $(PIPELINE)

pipeline-status:  ## サービス状態とキュー深度
	@echo "=== Containers ==="
	@docker compose ps --format 'table {{.Name}}\t{{.State}}\t{{.Status}}'
	@echo
	@echo "=== Queue depth ==="
	@for q in $(QUEUES); do \
	  printf "  %-22s " "$$q"; \
	  docker compose exec -T redis redis-cli XLEN "$$q" 2>/dev/null \
	    || echo "redis unreachable"; \
	done
	@echo
	@restarting=$$(docker compose ps --format json \
	  | jq -r 'select(.State=="restarting") | "  ! \(.Name): \(.Status)"'); \
	if [ -n "$$restarting" ]; then \
	  echo "=== Restarting services (要確認) ==="; \
	  echo "$$restarting"; \
	fi

pipeline-logs:  ## 直近ログ（make pipeline-logs SERVICE=scheduler で個別指定可）
	docker compose logs --tail=50 $(or $(SERVICE),$(WORKERS))

# -------------------------------------------
# DB マイグレーション（worker 停止 → migrate → 再起動の順を強制）
# -------------------------------------------

migrate-safe:  ## ORM/migration 変更を安全に反映
	docker compose stop $(WORKERS)
	docker compose exec backend alembic upgrade head
	docker compose up -d --force-recreate $(WORKERS)
	@sleep 10
	@$(MAKE) --no-print-directory pipeline-status

# 本番 Neon 用。docker を介さず host/CI から流し、owner URL (MIGRATION_DATABASE_URL)
# は runtime secret に置かない方針のため実行時に渡す (dev は migrate-safe)。
# 他の必須 Settings は .env から、DATABASE_URL は同 URL で満たす (接続は使わない)。
migrate-prod:  ## 本番 Neon に migration 適用（MIGRATION_DATABASE_URL を実行時に渡す）
	@test -n "$$MIGRATION_DATABASE_URL" \
	  || { echo "MIGRATION_DATABASE_URL is required (Neon owner role, append ?sslmode=require)"; exit 1; }
	cd backend && \
	  export DATABASE_URL="$$MIGRATION_DATABASE_URL" MIGRATION_DATABASE_URL="$$MIGRATION_DATABASE_URL" && \
	  uv run alembic upgrade head && \
	  uv run alembic current

# -------------------------------------------
# 設定検証
# -------------------------------------------

verify-env:  ## .env を実際の Settings() で検証（必須キー欠落・弱秘密を fail-fast）
	@docker compose run --rm --no-deps -T backend python -c \
	  "from app.config import Settings; Settings(); print('env: ok')" \
	  || (echo "env validation failed — see error above" && exit 1)

verify-config:  ## docker-compose.yml の env 補間を検証（$${VAR} の未解決を検出）
	@docker compose config --quiet && echo "compose: ok"

# -------------------------------------------
# 統合テスト経路（host pytest ↔ db-test / redis-test ephemeral infra）
# -------------------------------------------
# 本体 `db` / Redis は host 非露出 (red-team 対策) のため、host から実 infra を叩く
# 統合テストは別 compose の `db-test` / `redis-test` を立てて回す。終了時は `trap` で
# 必ず `down -v` し、tmpfs と合わせて毎回 fresh を保証する。
#
# 環境変数は Makefile が export するため、開発者の .env (dev DB 向け) を
# 一切汚染しない (pydantic-settings priority: OS env > .env)。
#
# DATABASE_URL は app role (`vector_app`) で接続して runtime と同形を保ち、
# MIGRATION_DATABASE_URL のみ owner role (`vector`) で conftest の
# `_ensure_test_database_once` が vector_test 作成 / extension / schema
# を確保するのに使う。

# project 名を fail-closed で検証する。`$(error)` は parse 時に全 target で
# 発火し `make help` も壊すため使わず、test-integration 経路の recipe 内で
# 2 段 case 検証する: (1) `vector-test-` prefix + 非空、(2) 文字種が compose
# project 名規則 [a-z0-9_-] のみ。prefix だけだと command-line override
# (例 `vector-test-x -p vector`) の空白 / 追加 flag を通し option 注入になり
# 得るため、文字種検証で構造的に閉じる。
test-integration-guard:
	@project="$(TEST_COMPOSE_PROJECT)"; \
	case "$$project" in \
	  vector-test-[a-z0-9_-]*) ;; \
	  *) echo "FATAL: TEST_COMPOSE_PROJECT='$$project' は 'vector-test-' で始まる非空の compose project 名である必要があります (dev stack 巻き添え削除を防ぐため中止)" >&2; exit 1 ;; \
	esac; \
	case "$$project" in \
	  *[!a-z0-9_-]*) echo "FATAL: TEST_COMPOSE_PROJECT='$$project' に不正な文字があります (option 注入防止のため中止)" >&2; exit 1 ;; \
	esac

# 算出された project 名を確認する internal target。`make -n test-integration` は
# recipe の `$(MAKE)` 行が recursive make 規約で -n 下でも実行され pytest まで
# 走るため dry-run に使えない。副作用のない本 target で project 名を確認する。
test-integration-print-project:
	@echo "$(TEST_COMPOSE_PROJECT)"

test-integration: test-integration-up  ## db-test / redis-test を立てて pytest -m integration を実行（trap で必ず後始末）
	@set -e; \
	trap '$(MAKE) --no-print-directory -C $(CURDIR) test-integration-down' EXIT; \
	port=$$($(TEST_COMPOSE) port db-test 5432 | sed 's/.*://'); \
	test -n "$$port" || { echo "FATAL: db-test の host port を解決できません" >&2; exit 1; }; \
	redis_port=$$($(TEST_COMPOSE) port redis-test 6379 | sed 's/.*://'); \
	test -n "$$redis_port" || { echo "FATAL: redis-test の host port を解決できません" >&2; exit 1; }; \
	cd backend && \
	  DATABASE_URL=postgresql+asyncpg://vector_app:test-app-password@127.0.0.1:$$port/vector \
	  MIGRATION_DATABASE_URL=postgresql+asyncpg://vector:test-vector-password@127.0.0.1:$$port/vector \
	  REDIS_URL=redis://127.0.0.1:$$redis_port/0 \
	  POSTGRES_AUTH_PASSWORD=test-auth-password \
	  POSTGRES_APP_PASSWORD=test-app-password \
	  POSTGRES_COLLECT_PASSWORD=test-collect-password \
	  INTERNAL_API_SECRET=test-only-collect-bootstrap-xxxxxxxxxxxx \
	  FRONTEND_URL=http://localhost:3000 \
	  INTERNAL_FRONTEND_BASE_URL=http://localhost:3000 \
	  uv run pytest -m integration $(PYTEST_ARGS)

test-integration-up: test-integration-guard  ## test infra を fresh 状態で起動（前回の残骸を down -v してから up --wait）
	$(TEST_COMPOSE) down -v --remove-orphans
	$(TEST_COMPOSE) up -d --wait db-test redis-test

test-integration-down: test-integration-guard  ## db-test を停止して volume / orphan も含めて削除
	$(TEST_COMPOSE) down -v --remove-orphans
