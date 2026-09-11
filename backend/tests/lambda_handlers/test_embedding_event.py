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
from app.lambda_handlers.embedding.event import (
    EmbeddingEventInvalidError,
    parse_assessed_in_scope_event,
)
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.publisher import EventEnvelope

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
    event = parse_assessed_in_scope_event(json.dumps(data))
    assert event.event_id == UUID(int=1)
    assert event.occurred_at == datetime(2026, 9, 7, 3, 0, 0, 123456, tzinfo=UTC)
    assert event.payload == ArticleAssessedInScope(
        curation_id=123, analyzed_article_id=456
    )
    with pytest.raises(ValidationError):
        event.schema_version = 2
    with pytest.raises(ValidationError):
        event.payload.curation_id = 9


def test_json_dump_normalizes_occurred_at_to_utc_z_with_fraction(data):
    """送信本文の日時は入力のoffsetに依らずUTCのZ表記にし、小数秒を保つ。"""
    event = ArticleAssessedInScopeEvent.model_validate(data)
    dumped = event.model_dump(mode="json")
    assert dumped["occurred_at"] == "2026-09-07T03:00:00.123456Z"
    assert dumped["event_id"] == data["event_id"]
    assert event.model_dump()["occurred_at"] == event.occurred_at


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
    "field", ["event_id", "event_type", "schema_version", "occurred_at", "payload"]
)
def test_missing_envelope_field_is_invalid(data, field):
    del data[field]
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(json.dumps(data))
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
        parse_assessed_in_scope_event(json.dumps(data))
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
        parse_assessed_in_scope_event(json.dumps(data))
    assert caught.value.reason.value == "invalid_payload"


def test_payload_unknown_field_is_rejected(data):
    data["payload"]["private-field"] = "private-value"
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(json.dumps(data))
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
        parse_assessed_in_scope_event(body)
    assert caught.value.reason.value == "invalid_json"
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("body", ["null", "[]", "1", "true", '"text"'])
def test_json_root_must_be_an_object(body):
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(body)
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
        parse_assessed_in_scope_event(json.dumps(data))
    assert caught.value.reason.value == reason


@pytest.mark.parametrize(
    "changes,expected",
    [
        (
            {"payload": {}},
            [
                ("payload.curation_id", "missing_required_field"),
                ("payload.analyzed_article_id", "missing_required_field"),
            ],
        ),
        (
            {"payload": {"curation_id": True, "analyzed_article_id": 0}},
            [
                ("payload.curation_id", "invalid_type"),
                ("payload.analyzed_article_id", "invalid_value"),
            ],
        ),
        ({"occurred_at": "2026-09-07T03:00:00"}, [("occurred_at", "invalid_value")]),
        ({"schema_version": 2}, [("schema_version", "unsupported_schema_version")]),
        ({"event_type": "future"}, [("event_type", "unsupported_event_type")]),
        ({"private-one": 1, "private-two": 2}, [("event", "unknown_field")]),
        (
            {
                "payload": {
                    "curation_id": 1,
                    "analyzed_article_id": 2,
                    "private-one": 1,
                    "private-two": 2,
                }
            },
            [("payload", "unknown_field")],
        ),
    ],
)
def test_validation_details_are_safe_and_deduplicated(data, changes, expected):
    from dataclasses import FrozenInstanceError

    data.update(changes)
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(json.dumps(data))
    error = caught.value
    assert [(issue.field.value, issue.code.value) for issue in error.issues] == expected
    assert "private-" not in str(error)
    assert "private-" not in repr(error.issues)
    assert error.__cause__ is None and error.__context__ is None
    with pytest.raises(FrozenInstanceError):
        error.issues[0].field = "private-field"


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
