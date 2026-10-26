"""補完への送信本文と取得イベント契約の接続を確認する。"""

from dataclasses import asdict
from datetime import datetime
from uuid import UUID

import pytest

from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
    IncompleteArticleRecordedEvent,
)
from app.outbox.event.envelope import EventEnvelope
from app.outbox.event.incomplete_recorded import build_incomplete_recorded_message

pytestmark = pytest.mark.unit


def test_invalid_event_raises_shared_contract_error_without_input():
    """共有契約が拒否した入力は、契約と同じ構築失敗の例外のまま、原因の連鎖も入力値も持たずに送出する。"""
    envelope = EventEnvelope(
        event_id=UUID("10000000-0000-0000-0000-000000000002"),
        event_type="article.incomplete_recorded",
        schema_version=1,
        occurred_at=datetime.fromisoformat("2026-09-12T03:00:00Z"),
        payload={
            "source_id": 1,
            "incomplete_article_id": "private-input",
            "private-field": "private-value",
        },
    )
    with pytest.raises(IncompleteArticleEventInvalidError) as validation:
        IncompleteArticleRecordedEvent.from_input(asdict(envelope))

    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        build_incomplete_recorded_message(envelope)

    assert caught.value.invalid == validation.value.invalid
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    diagnostics = str(caught.value) + repr(vars(caught.value))
    assert all(
        value not in diagnostics
        for value in ("private-input", "private-field", "private-value")
    )


def test_message_preserves_envelope_and_payload_contract():
    """送信本文はイベント契約の日時表現と両IDを保持する。"""
    import json

    envelope = EventEnvelope(
        event_id=UUID("10000000-0000-0000-0000-000000000002"),
        event_type="article.incomplete_recorded",
        schema_version=1,
        occurred_at=datetime.fromisoformat("2026-09-14T09:30:00+09:00"),
        payload={"source_id": 12, "incomplete_article_id": 34},
    )
    message = build_incomplete_recorded_message(envelope)
    assert message.event_id == envelope.event_id
    assert json.loads(message.body) == {
        "event_id": str(envelope.event_id),
        "event_type": "article.incomplete_recorded",
        "schema_version": 1,
        "occurred_at": "2026-09-14T09:30:00+09:00",
        "payload": {"source_id": 12, "incomplete_article_id": 34},
    }
