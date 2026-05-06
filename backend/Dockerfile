# syntax=docker/dockerfile:1.7

# --- Builder stage: install dependencies via uv ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- Runtime stage: slim Debian bookworm with copied venv ---
FROM python:3.13-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# non-root user (uid 1001 — frontend production stage の nextjs uid と揃える)。
# container 侵害時の権限拡大を構造的に防ぐため。port 8000 は unprivileged
# port なので uid 1001 で listen 可能。alembic upgrade / uvicorn / taskiq worker
# はいずれも file system write を発生させないため runtime での chown は不要。
RUN groupadd --system --gid 1001 backend \
 && useradd  --system --uid 1001 --gid backend backend

COPY --from=builder --chown=backend:backend /app/.venv /app/.venv
COPY --chown=backend:backend . .

USER backend

# Migration 経路と app 起動経路を構造的に分離する (PR4 / red-team C2 防御):
# - dev (docker-compose) は compose.yml の `command:` で migration → uvicorn を override
# - 本番 (Fly.io) は fly.toml の [deploy].release_command で alembic upgrade head を実行
# Dockerfile の CMD は uvicorn のみ。migration を起動経路に同居させない。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
