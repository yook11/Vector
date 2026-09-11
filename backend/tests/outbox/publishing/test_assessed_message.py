"""既存イベント契約と配送本文の接続を検証する。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.publisher import EventEnvelope


@pytest.fixture
def envelope():
    return EventEnvelope(
        UUID(int=1),
        "article.assessed_in_scope",
        1,
        datetime(2026, 9, 7, 3, tzinfo=UTC),
        {"curation_id": 123, "analyzed_article_id": 456},
    )


def test_builder_keeps_exact_existing_json_and_event_id(envelope):
    """保存済みの5項目を既存のJSON表現で出力し、再送でも同じ本文を返す。"""
    message = build_assessed_in_scope_message(envelope)
    assert message.event_id == envelope.event_id
    assert message.body == (
        '{"event_id": "00000000-0000-0000-0000-000000000001", '
        '"event_type": "article.assessed_in_scope", '
        '"schema_version": 1, '
        '"occurred_at": "2026-09-07T03:00:00Z", '
        '"payload": {"curation_id": 123, "analyzed_article_id": 456}}'
    )
    assert build_assessed_in_scope_message(envelope) == message
    assert message.body not in repr(message)
