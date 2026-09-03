"""Alembic専用engine factory。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.connection import create_app_engine
from app.db.iam import build_iam_password_provider
from app.db.migration.settings import MigrationSettings


def create_migration_engine(
    settings: MigrationSettings,
    **engine_kwargs: Any,
) -> AsyncEngine:
    """password方式とIAM方式を同じAlembic入口へ正規化する。"""
    provider = None
    if settings.db_iam_auth:
        if settings.aws_region is None:
            raise RuntimeError("AWS_REGION is required when DB_IAM_AUTH is enabled")
        provider = build_iam_password_provider(
            settings.migration_database_url,
            region=settings.aws_region,
            token_port=settings.rds_iam_auth_token_port,
        )
    return create_app_engine(
        settings.migration_database_url,
        application_name="vector-migration",
        password_provider=provider,
        **engine_kwargs,
    )
