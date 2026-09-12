"""用途別の入口が固定の配送先を共通実行へ渡すことを確認する。"""

from importlib import import_module
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.lambda_handlers.outbox_relay.settings import (
    AssessmentOutboxRelaySettings,
    EmbeddingOutboxRelaySettings,
)
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.curated_signal import build_curated_signal_message
from app.outbox.publishing.route import EventDeliveryRoute

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("handler_name", "settings_type", "queue_field", "event_type", "builder"),
    [
        (
            "embedding_handler",
            EmbeddingOutboxRelaySettings,
            "sqs_article_embedding_queue_url",
            "article.assessed_in_scope",
            build_assessed_in_scope_message,
        ),
        (
            "assessment_handler",
            AssessmentOutboxRelaySettings,
            "sqs_article_assessment_queue_url",
            "article.curated_signal",
            build_curated_signal_message,
        ),
    ],
)
def test_entry_passes_its_fixed_route(
    monkeypatch,
    handler_name,
    settings_type,
    queue_field,
    event_type,
    builder,
):
    """各入口は自分の設定・種別・本文生成処理を共通実行へ渡す。"""
    entry = import_module("app.lambda_handlers.outbox_relay.handler")
    settings = settings_type(
        env="test",
        database_url="postgresql+asyncpg://user@database.invalid/vector",
        db_iam_auth=False,
        aws_region="ap-northeast-1",
        **{queue_field: "https://sqs.invalid/selected-queue"},
    )
    monkeypatch.setattr(entry, settings_type.__name__, Mock(return_value=settings))
    run = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(entry, "run_relay", run)

    response = getattr(entry, handler_name)({}, None)

    run.assert_awaited_once_with(
        settings,
        EventDeliveryRoute(event_type, "https://sqs.invalid/selected-queue", builder),
    )
    assert response == {"status": "completed"}


def test_invalid_settings_prevent_execution(monkeypatch):
    """設定構築に失敗した起動では資源準備・実行へ進まない。"""
    entry = import_module("app.lambda_handlers.outbox_relay.handler")
    error = ValidationError.from_exception_data("EmbeddingOutboxRelaySettings", [])
    monkeypatch.setattr(entry, "EmbeddingOutboxRelaySettings", Mock(side_effect=error))
    run = AsyncMock()
    monkeypatch.setattr(entry, "run_relay", run)

    with pytest.raises(ValidationError) as caught:
        entry.embedding_handler({}, None)

    assert caught.value is error
    run.assert_not_called()
