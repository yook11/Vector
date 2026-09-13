"""Curationへの送信本文と共有イベント契約の接続を確認する。"""

from dataclasses import asdict
from datetime import datetime
from uuid import UUID

import pytest

from app.collection.events import (
    AnalyzableArticleCreatedEvent,
    AnalyzableEventValidationError,
)
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.errors import PublishEventInvalidError
from app.outbox.publishing.publisher import EventEnvelope

pytestmark = pytest.mark.unit


def test_invalid_event_preserves_safe_validation_details():
    """共有契約が拒否した入力を、同じ理由・詳細で入力を残さず送信エラーへ変換する。"""
    envelope = EventEnvelope(
        event_id=UUID("10000000-0000-0000-0000-000000000002"),
        event_type="article.analyzable_created",
        schema_version=1,
        occurred_at=datetime.fromisoformat("2026-09-12T03:00:00Z"),
        payload={
            "analyzable_article_id": "private-input",
            "private-field": "private-value",
        },
    )
    with pytest.raises(AnalyzableEventValidationError) as validation:
        AnalyzableArticleCreatedEvent.from_input(asdict(envelope))

    with pytest.raises(PublishEventInvalidError) as caught:
        build_analyzable_created_message(envelope)

    assert caught.value.reason.value == validation.value.failure.reason.value
    assert caught.value.issues == validation.value.failure.issues
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    diagnostics = str(caught.value) + repr(vars(caught.value))
    assert all(
        value not in diagnostics
        for value in ("private-input", "private-field", "private-value")
    )
