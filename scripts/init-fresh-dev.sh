#!/usr/bin/env bash
# Fresh dev volume を再現可能にセットアップする helper。
#
# 通常は `docker compose up -d --build` で十分 (docker-compose.yml の db-init-*
# 4 service が同等の処理を自動で行う)。本 helper は次のような場面で使う:
#   - docker compose を経由せず手動で順序を確認したいとき
#   - 既存 dev volume が壊れて db-init-* も走らないとき (volume 削除後の復旧)
#   - 個別 step だけ再実行したいとき
#
# 全 step は冪等 (CREATE SCHEMA IF NOT EXISTS / Better Auth CLI migrate /
# alembic upgrade head)。再実行しても破壊的影響はない。
set -euo pipefail

CLI_VERSION="${BETTER_AUTH_CLI_VERSION:-1.4.22}"

cd "$(dirname "$0")/.."

echo "==> 1/5 db を起動 (init script で vector_auth / vector_app role 作成)"
docker compose up -d --wait db

echo "==> 2/5 auth schema 作成 + vector_auth に一時 CREATE 権限付与"
docker compose exec -T db psql -U vector -d "${POSTGRES_DB:-vector}" -v ON_ERROR_STOP=1 <<'SQL'
CREATE SCHEMA IF NOT EXISTS auth;
GRANT USAGE, CREATE ON SCHEMA auth TO vector_auth;
SQL

echo "==> 3/5 Better Auth CLI migrate (auth.user/session/account/verification 作成)"
# Apple Silicon / aarch64 で @better-auth/cli@1.4.x が依存する better-sqlite3 が
# linux-arm64 prebuild を持たないため、--ignore-scripts で post-install を抑止する。
# CLI は pg/kysely しか load しないため機能影響なし。
docker compose up -d --no-deps frontend
docker compose exec -T frontend sh -c "
  set -e
  INSTALL_DIR=/tmp/ba-cli
  if [ ! -x \"\$INSTALL_DIR/node_modules/.bin/better-auth\" ]; then
    mkdir -p \"\$INSTALL_DIR\"
    cd \"\$INSTALL_DIR\"
    npm init -y >/dev/null
    npm install --ignore-scripts --no-fund --no-audit \"@better-auth/cli@${CLI_VERSION}\" >/dev/null
  fi
  cd /app
  yes y | \"\$INSTALL_DIR/node_modules/.bin/better-auth\" migrate --config src/lib/auth/auth.cli.ts
"

echo "==> 4/5 vector_auth から CREATE 権限を REVOKE (n3_grant_app_db_users の意図と整合)"
docker compose exec -T db psql -U vector -d "${POSTGRES_DB:-vector}" -v ON_ERROR_STOP=1 \
  -c "REVOKE CREATE ON SCHEMA auth FROM vector_auth;"

echo "==> 5/5 残り service を起動 (db-init-alembic が alembic upgrade head を流す)"
docker compose up -d --build

echo
echo "完了。docker compose ps で全 service が healthy であることを確認してください。"
