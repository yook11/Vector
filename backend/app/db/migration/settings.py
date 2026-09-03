"""通常アプリの秘密を読まないmigration専用設定。"""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.db.ssl import parse_sslmode

_PRODUCTION_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})


class MigrationSettings(BaseSettings):
    """migrationに不要なapplication secretを読み込まない設定。"""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    env: Literal["development", "production"] = "development"
    migration_database_url: str = Field(validation_alias="MIGRATION_DATABASE_URL")
    db_iam_auth: bool = False
    aws_region: str | None = None
    rds_iam_auth_token_port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        validation_alias="RDS_IAM_AUTH_TOKEN_PORT",
    )

    @model_validator(mode="after")
    def _validate_authentication_boundary(self) -> Self:
        url = make_url(self.migration_database_url)
        if self.db_iam_auth and self.aws_region is None:
            raise ValueError(
                "DB_IAM_AUTH is enabled but AWS_REGION is not set; "
                "the IAM auth token is signed per region"
            )
        if self.db_iam_auth and url.password is not None:
            raise ValueError(
                "DB_IAM_AUTH is enabled but MIGRATION_DATABASE_URL contains a password"
            )
        if self.env != "production":
            return self
        if not self.db_iam_auth:
            raise ValueError("production migration requires DB_IAM_AUTH=true")
        if url.username != "vector":
            raise ValueError("production migration database user must be vector")
        query_pairs = parse_qsl(
            urlsplit(self.migration_database_url).query,
            keep_blank_values=True,
        )
        if len(query_pairs) != 1 or query_pairs[0][0].lower() != "sslmode":
            raise ValueError(
                "production MIGRATION_DATABASE_URL permits only the sslmode query"
            )
        sslmode = parse_sslmode(self.migration_database_url)
        if sslmode not in _PRODUCTION_TLS_MODES:
            raise ValueError(
                "production MIGRATION_DATABASE_URL requires sslmode=require, "
                "verify-ca, or verify-full"
            )
        return self


def load_migration_settings() -> MigrationSettings:
    """process environmentからmigration設定だけを構築する。"""
    return MigrationSettings()
