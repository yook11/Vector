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
        migrate-safe verify-env verify-config

# サービス分類（変更する時はここだけ触る）
WORKERS  := worker-metadata worker-content worker-analysis worker-embedding scheduler
PIPELINE := backend $(WORKERS)
QUEUES   := pipeline:metadata pipeline:content pipeline:analysis pipeline:embedding

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
