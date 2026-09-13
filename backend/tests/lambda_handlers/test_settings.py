"""relayの起動に必要な環境変数から設定を構築できる条件を確認する。"""

import pytest
from pydantic import ValidationError

from app.lambda_handlers.outbox_relay.settings import (
    AssessmentOutboxRelaySettings,
    CurationOutboxRelaySettings,
    EmbeddingOutboxRelaySettings,
)

pytestmark = pytest.mark.unit

DATABASE_URL = "postgresql+asyncpg://vector_app@database.invalid/vector?sslmode=require"
QUEUE_FIELDS = ("sqs_article_embedding_queue_url",)


@pytest.fixture
def relay_environment(monkeypatch):
    for stage in ("COMPLETION", "CURATION", "ASSESSMENT", "EMBEDDING"):
        monkeypatch.delenv(f"SQS_ARTICLE_{stage}_QUEUE_URL", raising=False)
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


@pytest.mark.parametrize(
    ("settings_type", "queue_field"),
    [
        (EmbeddingOutboxRelaySettings, "sqs_article_embedding_queue_url"),
        (AssessmentOutboxRelaySettings, "sqs_article_assessment_queue_url"),
        (CurationOutboxRelaySettings, "sqs_article_curation_queue_url"),
    ],
)
def test_requires_only_its_destination(
    relay_environment, monkeypatch, settings_type, queue_field
):
    """各設定は自分のキューを必須とし、他工程のURLを要求も保持もしない。"""
    values = dict(relay_environment)
    values.pop("sqs_article_embedding_queue_url")
    monkeypatch.delenv("SQS_ARTICLE_EMBEDDING_QUEUE_URL")
    with pytest.raises(ValidationError) as caught:
        settings_type()
    assert [error["loc"] for error in caught.value.errors()] == [(queue_field,)]

    values[queue_field] = "https://sqs.invalid/destination"
    monkeypatch.setenv(queue_field.upper(), values[queue_field])
    assert settings_type().model_dump() == values


@pytest.mark.parametrize("field", ("database_url", "aws_region"))
def test_rejects_missing_required_setting(relay_environment, monkeypatch, field):
    """必須の接続設定が欠けている場合は構築できない。"""
    monkeypatch.delenv(field.upper())
    with pytest.raises(ValidationError) as caught:
        EmbeddingOutboxRelaySettings()
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
        EmbeddingOutboxRelaySettings()
