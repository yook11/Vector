"""Alembic 専用設定の認証境界。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.db.engine import create_migration_engine
from app.db.migration.settings import MigrationSettings

_RDS_URL = (
    "postgresql+asyncpg://vector@"
    "vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/vector?sslmode=require"
)


def test_migration_database_url_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="MIGRATION_DATABASE_URL"):
        MigrationSettings()


def test_development_accepts_password_authentication() -> None:
    settings = MigrationSettings(
        migration_database_url=(
            "postgresql+asyncpg://vector:local-password@db:5432/vector"
        )
    )
    assert settings.db_iam_auth is False


def test_production_requires_iam_authentication() -> None:
    with pytest.raises(ValidationError, match="DB_IAM_AUTH"):
        MigrationSettings(env="production", migration_database_url=_RDS_URL)


def test_production_rejects_password() -> None:
    with pytest.raises(ValidationError, match="password"):
        MigrationSettings(
            env="production",
            db_iam_auth=True,
            aws_region="ap-northeast-1",
            migration_database_url=_RDS_URL.replace("vector@", "vector:leftover@"),
        )


@pytest.mark.parametrize(
    "extra_query",
    ["password=leftover", "password=", "application_name=secret"],
)
def test_production_rejects_non_ssl_query_parameters(extra_query: str) -> None:
    with pytest.raises(ValidationError, match="only the sslmode query"):
        MigrationSettings(
            env="production",
            db_iam_auth=True,
            aws_region="ap-northeast-1",
            migration_database_url=f"{_RDS_URL}&{extra_query}",
        )


def test_production_requires_vector_user() -> None:
    with pytest.raises(ValidationError, match="vector"):
        MigrationSettings(
            env="production",
            db_iam_auth=True,
            aws_region="ap-northeast-1",
            migration_database_url=_RDS_URL.replace("vector@", "vector_master@"),
        )


@pytest.mark.parametrize("sslmode", [None, "disable", "allow", "prefer"])
def test_production_requires_verified_tls(sslmode: str | None) -> None:
    url = _RDS_URL.partition("?")[0]
    if sslmode is not None:
        url = f"{url}?sslmode={sslmode}"
    with pytest.raises(ValidationError, match="sslmode"):
        MigrationSettings(
            env="production",
            db_iam_auth=True,
            aws_region="ap-northeast-1",
            migration_database_url=url,
        )


def test_iam_auth_requires_region() -> None:
    with pytest.raises(ValidationError, match="AWS_REGION"):
        MigrationSettings(
            env="production",
            db_iam_auth=True,
            migration_database_url=_RDS_URL,
        )


def test_token_port_override_is_validated() -> None:
    with pytest.raises(ValidationError, match="rds_iam_auth_token_port"):
        MigrationSettings(
            migration_database_url=_RDS_URL,
            rds_iam_auth_token_port=70000,
        )


def test_production_accepts_passwordless_vector_iam_url() -> None:
    settings = MigrationSettings(
        env="production",
        db_iam_auth=True,
        aws_region="ap-northeast-1",
        migration_database_url=_RDS_URL,
        rds_iam_auth_token_port=5432,
    )
    assert settings.rds_iam_auth_token_port == 5432


def test_local_password_migration_engine_does_not_install_token_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_engine(url: str, **kwargs: object) -> object:
        captured.update(url=url, **kwargs)
        return object()

    monkeypatch.setattr("app.db.engine._create_engine", fake_engine)
    settings = MigrationSettings(
        migration_database_url="postgresql+asyncpg://vector:local@db:5432/vector"
    )
    create_migration_engine(settings)

    assert captured["url"] == settings.migration_database_url
    assert captured["password_provider"] is None


def test_iam_migration_engine_passes_token_signing_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    captured: dict[str, object] = {}

    def fake_provider(url: str, **kwargs: object) -> object:
        captured.update(provider_url=url, **kwargs)
        return sentinel

    def fake_engine(url: str, **kwargs: object) -> object:
        captured.update(engine_url=url, **kwargs)
        return object()

    monkeypatch.setattr("app.db.engine.build_iam_password_provider", fake_provider)
    monkeypatch.setattr("app.db.engine._create_engine", fake_engine)
    settings = MigrationSettings(
        env="production",
        db_iam_auth=True,
        aws_region="ap-northeast-1",
        migration_database_url=_RDS_URL,
        rds_iam_auth_token_port=15432,
    )
    create_migration_engine(settings)

    assert captured == {
        "provider_url": _RDS_URL,
        "region": "ap-northeast-1",
        "token_port": 15432,
        "engine_url": _RDS_URL,
        "application_name": "vector-migration",
        "password_provider": sentinel,
    }
