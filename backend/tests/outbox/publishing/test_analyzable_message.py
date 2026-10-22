"""Curationへの送信本文と共有イベント契約の接続を確認する。"""

from dataclasses import asdict
from datetime import datetime
from uuid import UUID

import pytest

from app.collection.events import (
    AnalyzableArticleCreatedEvent,
    AnalyzableEventInvalidError,
)
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.contracts import EventEnvelope

pytestmark = pytest.mark.unit


def test_invalid_event_raises_shared_contract_error_without_input():
    """共有契約が拒否した入力は、契約と同じ構築失敗の例外のまま、原因の連鎖も入力値も持たずに送出する。"""
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
    with pytest.raises(AnalyzableEventInvalidError) as validation:
        AnalyzableArticleCreatedEvent.from_input(asdict(envelope))

    with pytest.raises(AnalyzableEventInvalidError) as caught:
        build_analyzable_created_message(envelope)

    assert caught.value.invalid == validation.value.invalid
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    diagnostics = str(caught.value) + repr(vars(caught.value))
    assert all(
        value not in diagnostics
        for value in ("private-input", "private-field", "private-value")
    )
