#!/bin/sh
# Create application-level Postgres roles for schema-scoped access (red-team AUTH-N4).
#
# This script runs once when the db volume is first initialized
# (Postgres docker-entrypoint-initdb.d mechanism).
# For existing dev volumes, run the equivalent SQL manually:
#   docker compose exec db psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "CREATE ROLE vector_auth    WITH LOGIN PASSWORD '...'"
#   docker compose exec db psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "CREATE ROLE vector_app     WITH LOGIN PASSWORD '...'"
#   docker compose exec db psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "CREATE ROLE vector_collect WITH LOGIN PASSWORD '...'"
# then run `alembic upgrade head` to apply GRANT migration.

set -e

if [ -z "$POSTGRES_AUTH_PASSWORD" ]; then
  echo "ERROR: POSTGRES_AUTH_PASSWORD is not set" >&2
  exit 1
fi

if [ -z "$POSTGRES_APP_PASSWORD" ]; then
  echo "ERROR: POSTGRES_APP_PASSWORD is not set" >&2
  exit 1
fi

if [ -z "$POSTGRES_COLLECT_PASSWORD" ]; then
  echo "ERROR: POSTGRES_COLLECT_PASSWORD is not set" >&2
  exit 1
fi

# psql の :'variable' 置換は dollar-quoted block (DO $$ ... $$) の内側では
# 効かない (psql が $$ 以下を opaque な string literal として扱うため)。
# よって \gexec で「SQL を生成 → 実行」の 2 段階パターンを使い、:'variable'
# を通常 SQL 中に置く。idempotency は WHERE NOT EXISTS で確保。format(%L)
# で SQL リテラルとして安全にエスケープする。
psql -v ON_ERROR_STOP=1 \
     -v auth_password="$POSTGRES_AUTH_PASSWORD" \
     -v app_password="$POSTGRES_APP_PASSWORD" \
     -v collect_password="$POSTGRES_COLLECT_PASSWORD" \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" <<-'EOSQL'
SELECT format('CREATE ROLE vector_auth WITH LOGIN PASSWORD %L', :'auth_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vector_auth')
\gexec
SELECT format('CREATE ROLE vector_app WITH LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vector_app')
\gexec
SELECT format('CREATE ROLE vector_collect WITH LOGIN PASSWORD %L', :'collect_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vector_collect')
\gexec
EOSQL

echo "Created Postgres app roles (vector_auth, vector_app, vector_collect) — GRANT applied via alembic migration."
