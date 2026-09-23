"""Assessmentへの送信本文と共有イベント契約の接続を確認する。"""

from dataclasses import asdict
from datetime import datetime
from uuid import UUID

import pytest

from app.analysis.curation.events import (
    ArticleCuratedSignalEvent,
    CuratedEventInvalidError,
)
from app.lambda_handlers.sqs.records import SqsRecord
from app.outbox.publishing.curated_signal import build_curated_signal_message
from app.outbox.publishing.errors import PublishEventInvalidError
from app.outbox.publishing.publisher import EventEnvelope

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
        SqsRecord(message_id="id", body=message.body).parse_json()
    )

    assert message.event_id == envelope.event_id
    assert received.model_dump() == asdict(envelope)
    assert '"occurred_at": "2026-09-12T03:00:00.123456Z"' in message.body


def test_invalid_event_preserves_safe_validation_details():
    """共有契約が拒否した入力を、同じ理由・詳細で入力を残さず送信エラーへ変換する。"""
    envelope = EventEnvelope(
        event_id=UUID("10000000-0000-0000-0000-000000000002"),
        event_type="article.curated_signal",
        schema_version=1,
        occurred_at=datetime.fromisoformat("2026-09-12T03:00:00Z"),
        payload={"curation_id": "private-input", "analyzable_article_id": 456},
    )
    with pytest.raises(CuratedEventInvalidError) as validation:
        ArticleCuratedSignalEvent.from_input(asdict(envelope))

    with pytest.raises(PublishEventInvalidError) as caught:
        build_curated_signal_message(envelope)

    assert caught.value.reason.value == validation.value.invalid.reason.value
    assert caught.value.issues == validation.value.invalid.issues
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-input" not in str(caught.value)
