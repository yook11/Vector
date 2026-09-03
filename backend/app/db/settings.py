"""通常アプリが使うDB接続設定と起動時検証。"""

from __future__ import annotations

from typing import Self
from urllib.parse import urlparse

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import ArgumentError

from app.db.ssl import parse_sslmode

_KNOWN_WEAK_DATABASE_URL_PATTERNS = frozenset(
    {
        "vector_app:vector_app",
        "vector_auth:vector_auth",
        "<set-strong-password",
    }
)
_DATABASE_URL_ENV_NAMES = {
    "database_url": "DATABASE_URL",
    "migration_database_url": "MIGRATION_DATABASE_URL",
    "auth_retention_database_url": "AUTH_RETENTION_DATABASE_URL",
}
_PRODUCTION_REQUIRED_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})


class DatabaseSettings(BaseSettings):
    """アプリ全体設定へ平坦なDB設定APIを提供する基底。"""

    model_config = SettingsConfigDict(hide_input_in_errors=True)

    database_url: str
    db_iam_auth: bool = False
    migration_database_url: str | None = None
    auth_retention_database_url: str | None = None
    postgres_auth_password: SecretStr | None = None
    postgres_app_password: SecretStr | None = None
    postgres_collect_password: SecretStr | None = None

    @field_validator(
        "database_url", "migration_database_url", "auth_retention_database_url"
    )
    @classmethod
    def _validate_database_url(cls, v: str | None, info: ValidationInfo) -> str | None:
        """DB接続文字列に公開済defaultやplaceholderが残らないことを強制する。"""
        if v is None:
            return v
        field_name = info.field_name
        if field_name is None:
            raise ValueError("internal error: missing field name in validator info")
        env_name = _DATABASE_URL_ENV_NAMES[field_name]
        for pattern in _KNOWN_WEAK_DATABASE_URL_PATTERNS:
            if pattern in v:
                raise ValueError(
                    f"{env_name} contains a known dev placeholder/weak password "
                    f"({pattern!r}); use a strong password generated with "
                    "`openssl rand -hex 32` and configure via .env"
                )
        return v

    @model_validator(mode="after")
    def _reject_password_when_iam_auth(self) -> Self:
        """IAM認証時にruntime URLのpasswordを拒否する。"""
        if not self.db_iam_auth:
            return self
        for field_name in ("database_url", "auth_retention_database_url"):
            raw: str | None = getattr(self, field_name)
            if raw is None:
                continue
            if urlparse(raw).password is not None:
                env_name = _DATABASE_URL_ENV_NAMES[field_name]
                raise ValueError(
                    f"DB_IAM_AUTH is enabled but {env_name} contains a password; "
                    "remove it (the IAM auth token replaces it)"
                )
        return self

    @model_validator(mode="after")
    def _require_region_when_iam_auth(self) -> Self:
        """IAM認証に必要なregionを起動時に要求する。"""
        if self.db_iam_auth and getattr(self, "aws_region", None) is None:
            raise ValueError(
                "DB_IAM_AUTH is enabled but AWS_REGION is not set; "
                "the IAM auth token is signed per region"
            )
        return self

    @model_validator(mode="after")
    def _require_ssl_in_production(self) -> Self:
        """productionの全DB接続文字列にTLS sslmodeを強制する。"""
        if getattr(self, "env", "development") != "production":
            return self
        for name, url in (
            ("DATABASE_URL", self.database_url),
            ("MIGRATION_DATABASE_URL", self.migration_database_url),
            ("AUTH_RETENTION_DATABASE_URL", self.auth_retention_database_url),
        ):
            if url is None:
                continue
            try:
                sslmode = parse_sslmode(url)
            except ArgumentError as exc:
                raise ValueError(f"{name} is not a parseable connection URL") from exc
            if sslmode not in _PRODUCTION_REQUIRED_SSLMODES:
                raise ValueError(
                    f"in production {name} must use a TLS sslmode "
                    f"({sorted(_PRODUCTION_REQUIRED_SSLMODES)}), got {sslmode!r}; "
                    "append `?sslmode=require` (connections to Neon cross the "
                    "public internet)"
                )
        return self
