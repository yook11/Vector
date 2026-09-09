"""relayの起動に必要な環境変数から設定を構築できる条件を確認する。"""

import pytest
from pydantic import ValidationError

from app.lambda_handlers.settings import OutboxRelaySettings

pytestmark = pytest.mark.unit

DATABASE_URL = "postgresql+asyncpg://vector_app@database.invalid/vector?sslmode=require"
QUEUE_FIELDS = tuple(
    f"sqs_article_{stage}_queue_url"
    for stage in ("completion", "curation", "assessment", "embedding")
)


@pytest.fixture
def relay_environment(monkeypatch):
    values = {
        "env": "production",
        "database_url": DATABASE_URL,
        "db_iam_auth": True,
        "aws_region": "ap-northeast-1",
        **{field: f"https://sqs.invalid/{field}" for field in QUEUE_FIELDS},
    }
    for field, value in values.items():
        monkeypatch.setenv(field.upper(), str(value))
    for field in (
        "MIGRATION_DATABASE_URL",
        "AUTH_RETENTION_DATABASE_URL",
        "POSTGRES_AUTH_PASSWORD",
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_COLLECT_PASSWORD",
    ):
        monkeypatch.delenv(field, raising=False)
    return values


def test_constructs_settings_with_valid_values(relay_environment):
    """正しい設定値からOutboxRelaySettingsを構築できる。"""
    settings = OutboxRelaySettings()
    assert settings.model_dump() == relay_environment


@pytest.mark.parametrize("field", ("database_url", "aws_region", *QUEUE_FIELDS))
def test_rejects_missing_required_setting(relay_environment, monkeypatch, field):
    """必須の接続設定が欠けている場合は構築できない。"""
    monkeypatch.delenv(field.upper())
    with pytest.raises(ValidationError) as caught:
        OutboxRelaySettings()
    assert any(
        error["loc"] == (field,) and error["type"] == "missing"
        for error in caught.value.errors()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *((field, "") for field in QUEUE_FIELDS),
        ("aws_region", ""),
        ("env", "invalid-environment"),
        ("db_iam_auth", "invalid-boolean"),
        ("database_url", "not-a-database-url"),
        ("database_url", DATABASE_URL.split("?")[0]),
        (
            "database_url",
            DATABASE_URL.replace("vector_app@", "vector_app:private-sentinel@"),
        ),
    ],
)
def test_rejects_invalid_relay_setting(relay_environment, monkeypatch, field, value):
    """空の接続先や不正な値、本番接続のTLS・IAM条件違反を拒否する。"""
    monkeypatch.setenv(field.upper(), value)
    with pytest.raises(ValidationError):
        OutboxRelaySettings()
