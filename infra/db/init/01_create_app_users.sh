#!/bin/sh
# Create application-level Postgres roles for schema-scoped access (red-team AUTH-N4).
#
# This script runs once when the db volume is first initialized
# (Postgres docker-entrypoint-initdb.d mechanism).
# For existing dev volumes, run the equivalent SQL manually:
#   docker compose exec db psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "CREATE ROLE vector_auth WITH LOGIN PASSWORD '...'"
#   docker compose exec db psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "CREATE ROLE vector_app  WITH LOGIN PASSWORD '...'"
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

psql -v ON_ERROR_STOP=1 \
     -v auth_password="$POSTGRES_AUTH_PASSWORD" \
     -v app_password="$POSTGRES_APP_PASSWORD" \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" <<-'EOSQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vector_auth') THEN
    EXECUTE format('CREATE ROLE vector_auth WITH LOGIN PASSWORD %L', :'auth_password');
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vector_app') THEN
    EXECUTE format('CREATE ROLE vector_app WITH LOGIN PASSWORD %L', :'app_password');
  END IF;
END $$;
EOSQL

echo "Created Postgres app roles (vector_auth, vector_app) — GRANT applied via alembic migration."
