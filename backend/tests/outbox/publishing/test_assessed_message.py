"""既存イベント契約と配送本文の接続を検証する。"""

import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.lambda_handlers.embedding.event import (
    EmbeddingEventInvalidError,
    parse_assessed_in_scope_event,
)
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


@pytest.fixture
def data():
    return {
        "event_id": "00000000-0000-0000-0000-000000000001",
        "event_type": "article.assessed_in_scope",
        "schema_version": 1,
        "occurred_at": "2026-09-07T12:00:00.123456+09:00",
        "payload": {"curation_id": 123, "analyzed_article_id": 456},
    }


def test_sender_body_round_trip_keeps_identity_time_and_payload(data):
    """実本文生成から受信検証まで接続し、ID・時刻・payloadの一致を確認する。"""
    sent = ArticleAssessedInScopeEvent(
        event_id=UUID(data["event_id"]),
        event_type=data["event_type"],
        schema_version=1,
        occurred_at=datetime(
            2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
        ),
        payload=data["payload"],
    )
    event = parse_assessed_in_scope_event(
        build_assessed_in_scope_message(
            EventEnvelope(
                sent.event_id,
                sent.event_type,
                sent.schema_version,
                sent.occurred_at,
                sent.payload.model_dump(),
            )
        ).body
    )
    assert event.event_id == sent.event_id
    assert event.occurred_at == sent.occurred_at
    assert event.payload == sent.payload
    assert ArticleAssessedInScopeEvent.model_validate(data) == event


@pytest.mark.parametrize(
    "changes",
    [
        {"payload": {"curation_id": "private-input", "analyzed_article_id": 2}},
        {"schema_version": 2},
        {"event_type": "private-event"},
        {
            "event_type": "private-event",
            "occurred_at": "2026-09-10T00:00:00",
            "payload": {},
        },
    ],
)
def test_sender_and_receiver_share_validation_details(data, changes):
    """本文生成と受信検証が同じ理由・詳細を返し、入力の原因連鎖を保持しない。"""
    from app.outbox.publishing.errors import PublishEventInvalidError

    data.update(changes)
    with pytest.raises(EmbeddingEventInvalidError) as received:
        parse_assessed_in_scope_event(json.dumps(data))
    with pytest.raises(PublishEventInvalidError) as caught:
        build_assessed_in_scope_message(
            EventEnvelope(
                event_id=UUID(data["event_id"]),
                event_type=data["event_type"],
                schema_version=data["schema_version"],
                occurred_at=datetime.fromisoformat(data["occurred_at"]),
                payload=data["payload"],
            )
        )
    sent = caught.value
    assert sent.reason.value == received.value.reason.value
    assert sent.issues == received.value.issues
    assert sent.__cause__ is None and sent.__context__ is None
    assert "private-" not in str(sent)
