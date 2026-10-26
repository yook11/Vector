"""Assessmentへの送信本文と共有イベント契約の接続を確認する。"""

from dataclasses import asdict
from datetime import datetime
from uuid import UUID

import pytest

from app.analysis.curation.events import (
    ArticleCuratedSignalEvent,
    CuratedEventInvalidError,
)
from app.aws.sqs.message import SqsMessage
from app.outbox.event.curated_signal import build_curated_signal_message
from app.outbox.event.envelope import EventEnvelope

pytestmark = pytest.mark.unit


def test_message_round_trip_preserves_stored_event():
    """保存済みの全項目を送信本文から復元でき、小数秒も失わない。"""
    envelope = EventEnvelope(
        event_id=UUID("10000000-0000-0000-0000-000000000001"),
        event_type="article.curated_signal",
        schema_version=1,
        occurred_at=datetime.fromisoformat("2026-09-12T12:00:00.123456+09:00"),
        payload={"curation_id": 123, "analyzable_article_id": 456},
    )

    message = build_curated_signal_message(envelope)
    received = ArticleCuratedSignalEvent.from_input(
        SqsMessage(
            receipt_handle="receipt-handle", message_id="id", body=message.body
        ).parse_json()
    )

    assert message.event_id == envelope.event_id
    assert received.model_dump() == asdict(envelope)
    assert '"occurred_at": "2026-09-12T03:00:00.123456Z"' in message.body


def test_invalid_event_raises_shared_contract_error_without_input():
    """共有契約が拒否した入力は、契約と同じ構築失敗の例外のまま、原因の連鎖も入力値も持たずに送出する。"""
    envelope = EventEnvelope(
        event_id=UUID("10000000-0000-0000-0000-000000000002"),
        event_type="article.curated_signal",
        schema_version=1,
        occurred_at=datetime.fromisoformat("2026-09-12T03:00:00Z"),
        payload={"curation_id": "private-input", "analyzable_article_id": 456},
    )
    with pytest.raises(CuratedEventInvalidError) as validation:
        ArticleCuratedSignalEvent.from_input(asdict(envelope))

    with pytest.raises(CuratedEventInvalidError) as caught:
        build_curated_signal_message(envelope)

    assert caught.value.invalid == validation.value.invalid
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-input" not in str(caught.value)
