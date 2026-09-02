"""Alembic専用の最小設定層。"""

from __future__ import annotations

import re
from typing import Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.db_ssl import parse_sslmode

_PRODUCTION_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
MIGRATION_RUNNER_PROTOCOL_VERSION = 1


def require_revision_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}", value) is None or value in {
        "base",
        "head",
        "heads",
    }:
        raise ValueError("a concrete migration revision ID is required")
    return value


class MigrationRunnerRequest(BaseSettings):
    """実行要求をDB接続設定から分離し、通常のAlembicにmodeを要求しない。"""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
        frozen=True,
    )

    protocol_version: Literal[1] = Field(validation_alias="MIGRATION_PROTOCOL_VERSION")
    mode: Literal["expand", "contract", "verify"] = Field(
        validation_alias="MIGRATION_MODE"
    )
    expected_start_revision: str | None = Field(
        default=None, validation_alias="MIGRATION_EXPECTED_START_REVISION"
    )
    target_revision: str = Field(validation_alias="MIGRATION_TARGET_REVISION")
    migration_tree_oid: str = Field(validation_alias="MIGRATION_TREE_OID")

    @field_validator("protocol_version", mode="before")
    @classmethod
    def _require_protocol(cls, value: object) -> int:
        if value == str(MIGRATION_RUNNER_PROTOCOL_VERSION) or (
            type(value) is int and value == MIGRATION_RUNNER_PROTOCOL_VERSION
        ):
            return MIGRATION_RUNNER_PROTOCOL_VERSION
        raise ValueError("unsupported migration runner protocol")

    @field_validator("target_revision", "expected_start_revision")
    @classmethod
    def _require_revision(cls, value: str | None) -> str | None:
        return require_revision_id(value) if value is not None else None

    @field_validator("migration_tree_oid")
    @classmethod
    def _require_tree_oid(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("a 40-digit lowercase migration tree OID is required")
        return value

    @model_validator(mode="after")
    def _require_expected_start(self) -> Self:
        if self.mode != "verify" and self.expected_start_revision is None:
            raise ValueError("expand and contract require an expected start revision")
        return self


def load_migration_runner_request() -> MigrationRunnerRequest:
    return MigrationRunnerRequest()


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
