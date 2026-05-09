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
        migrate-safe verify-env verify-config \
        test-integration test-integration-up test-integration-down

# サービス分類（変更する時はここだけ触る）
WORKERS  := worker-fetch worker-analysis worker-embedding worker-insights scheduler
PIPELINE := backend $(WORKERS)
QUEUES   := pipeline:metadata pipeline:content pipeline:analysis pipeline:embedding digest briefing

# 統合テスト専用 Postgres を立てる compose の呼び出し前置詞。
# `-p vector-test` で project 名を本体 compose から分離する。-p 省略時は
# default project が本体と同じになり、`down --remove-orphans` が本体側
# container を巻き添えで消す危険があるため必須。
TEST_COMPOSE := docker compose -p vector-test -f docker-compose.test.yml

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
# 統合テスト経路（host pytest ↔ db-test ephemeral Postgres）
# -------------------------------------------
# 本体 `db` は host 非露出 (red-team 対策) のため、host から実 DB を叩く
# 統合テストは別 compose の `db-test` を立てて回す。終了時は `trap` で
# 必ず `down -v` し、tmpfs と合わせて毎回 fresh を保証する。
#
# 環境変数は Makefile が export するため、開発者の .env (dev DB 向け) を
# 一切汚染しない (pydantic-settings priority: OS env > .env)。
#
# DATABASE_URL は app role (`vector_app`) で接続して runtime と同形を保ち、
# MIGRATION_DATABASE_URL のみ owner role (`vector`) で conftest の
# `_ensure_test_database_once` が vector_test 作成 / extension / schema
# を確保するのに使う。

test-integration: test-integration-up  ## db-test を立てて pytest -m integration を実行（trap で必ず後始末）
	@set -e; \
	trap '$(MAKE) --no-print-directory -C $(CURDIR) test-integration-down' EXIT; \
	cd backend && \
	  DATABASE_URL=postgresql+asyncpg://vector_app:test-app-password@127.0.0.1:5433/vector \
	  MIGRATION_DATABASE_URL=postgresql+asyncpg://vector:test-vector-password@127.0.0.1:5433/vector \
	  POSTGRES_AUTH_PASSWORD=test-auth-password \
	  POSTGRES_APP_PASSWORD=test-app-password \
	  INTERNAL_API_SECRET=test-only-collect-bootstrap-xxxxxxxxxxxx \
	  FRONTEND_URL=http://localhost:3000 \
	  INTERNAL_FRONTEND_BASE_URL=http://localhost:3000 \
	  uv run pytest -m integration $(PYTEST_ARGS)

test-integration-up:  ## db-test を fresh 状態で起動（前回の残骸を down -v してから up --wait）
	$(TEST_COMPOSE) down -v --remove-orphans
	$(TEST_COMPOSE) up -d --wait db-test

test-integration-down:  ## db-test を停止して volume / orphan も含めて削除
	$(TEST_COMPOSE) down -v --remove-orphans
