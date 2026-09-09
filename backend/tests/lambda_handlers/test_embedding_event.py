"""送受信の共通契約と、入力本文を公開しない検証境界を確認する。"""

import json
import traceback
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
)
from app.lambda_handlers.embedding_event import (
    EmbeddingEventInvalidError,
    parse_embedding_event,
)
from app.outbox.publishing.publisher import EventEnvelope
from app.outbox.sqs.message import SqsMessage

pytestmark = pytest.mark.unit


@pytest.fixture
def data():
    return {
        "event_id": "00000000-0000-0000-0000-000000000001",
        "event_type": "article.assessed_in_scope",
        "schema_version": 1,
        "occurred_at": "2026-09-07T12:00:00.123456+09:00",
        "payload": {"curation_id": 123, "analyzed_article_id": 456},
    }


def test_restores_legacy_body_and_typed_payload(data):
    event = parse_embedding_event(json.dumps(data))
    assert event.event_id == UUID(int=1)
    assert event.occurred_at == datetime(2026, 9, 7, 3, 0, 0, 123456, tzinfo=UTC)
    assert event.payload == ArticleAssessedInScope(
        curation_id=123, analyzed_article_id=456
    )
    with pytest.raises(ValidationError):
        event.schema_version = 2
    with pytest.raises(ValidationError):
        event.payload.curation_id = 9


def test_sender_body_round_trip_keeps_identity_time_and_payload(data):
    envelope = EventEnvelope(
        event_id=UUID(data["event_id"]),
        event_type=data["event_type"],
        schema_version=1,
        occurred_at=datetime(
            2026, 9, 7, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
        ),
        payload=data["payload"],
    )
    event = parse_embedding_event(SqsMessage.from_envelope(envelope).body)
    assert event.event_id == envelope.event_id
    assert event.occurred_at == envelope.occurred_at
    assert event.payload.model_dump() == envelope.payload
    assert ArticleAssessedInScopeEvent.model_validate(data) == event


@pytest.mark.parametrize(
    "field", ["event_id", "event_type", "schema_version", "occurred_at", "payload"]
)
def test_missing_envelope_field_is_invalid(data, field):
    del data[field]
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_embedding_event(json.dumps(data))
    assert caught.value.reason.value == "invalid_envelope"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("event_id", "private-invalid-uuid", "invalid_envelope"),
        ("event_id", 1, "invalid_envelope"),
        ("event_type", 1, "invalid_envelope"),
        ("event_type", "future.event", "unsupported_event_type"),
        ("schema_version", "1", "invalid_envelope"),
        ("schema_version", True, "invalid_envelope"),
        ("schema_version", 1.0, "invalid_envelope"),
        ("schema_version", 0, "unsupported_schema_version"),
        ("schema_version", 2, "unsupported_schema_version"),
        ("occurred_at", "private-invalid-time", "invalid_envelope"),
        ("occurred_at", "2026-09-07T03:00:00", "invalid_envelope"),
        ("occurred_at", "2026-09-07", "invalid_envelope"),
        ("occurred_at", 1788746400, "invalid_envelope"),
        ("payload", None, "invalid_envelope"),
        ("payload", [], "invalid_envelope"),
        ("payload", "private-input", "invalid_envelope"),
        ("payload", {}, "invalid_payload"),
        ("extra", "private-extra", "invalid_envelope"),
    ],
)
def test_shared_contract_and_parser_reject_invalid_values(data, field, value, reason):
    data[field] = value
    with pytest.raises(ValidationError):
        ArticleAssessedInScopeEvent.model_validate(data)
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_embedding_event(json.dumps(data))
    assert caught.value.reason.value == reason
    assert "private-" not in str(caught.value)
    assert "private-" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("field", ["curation_id", "analyzed_article_id"])
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", None])
def test_payload_ids_are_strict_positive_integers(data, field, value):
    data["payload"][field] = value
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_embedding_event(json.dumps(data))
    assert caught.value.reason.value == "invalid_payload"


def test_payload_unknown_field_is_rejected(data):
    data["payload"]["private-field"] = "private-value"
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_embedding_event(json.dumps(data))
    assert caught.value.reason.value == "invalid_payload"
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "{",
        '{"private":',
        "NaN",
        "Infinity",
        "-Infinity",
        '{"x":1,"x":2}',
        '{"payload":{"x":1,"x":2}}',
        '{"payload":{"x":NaN}}',
    ],
)
def test_rejects_invalid_json_without_retaining_input(body):
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_embedding_event(body)
    assert caught.value.reason.value == "invalid_json"
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("body", ["null", "[]", "1", "true", '"text"'])
def test_json_root_must_be_an_object(body):
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_embedding_event(body)
    assert caught.value.reason.value == "invalid_envelope"


@pytest.mark.parametrize(
    "updates,reason",
    [
        (
            {
                "event_id": "invalid",
                "event_type": "future",
                "schema_version": 2,
                "payload": {},
            },
            "invalid_envelope",
        ),
        (
            {"event_type": "future", "schema_version": 2, "payload": {}},
            "unsupported_event_type",
        ),
        ({"schema_version": 2, "payload": {}}, "unsupported_schema_version"),
    ],
)
def test_error_reason_precedence(data, updates, reason):
    data.update(updates)
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_embedding_event(json.dumps(data))
    assert caught.value.reason.value == reason
