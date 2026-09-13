"""記事完成の共有イベント契約と、安全な検証詳細を確認する。"""

import traceback
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.collection.events import (
    AnalyzableArticleCreated,
    AnalyzableArticleCreatedEvent,
    AnalyzableEventValidationError,
)

pytestmark = pytest.mark.unit

RECEIVED_STRINGS = {
    "event_id": "00000000-0000-0000-0000-000000000001",
    "event_type": "article.analyzable_created",
    "schema_version": 1,
    "occurred_at": "2026-09-12T09:00:00.123456+09:00",
    "payload": {"analyzable_article_id": 3},
}
TYPED_VALUES = {
    "event_id": UUID(int=1),
    "event_type": "article.analyzable_created",
    "schema_version": 1,
    "occurred_at": datetime(
        2026, 9, 12, 9, 0, 0, 123456, tzinfo=timezone(timedelta(hours=9))
    ),
    "payload": {"analyzable_article_id": 3},
}


def test_from_input_accepts_received_strings():
    """受信したUUID・日時の文字列を復元し、既存のpayload型で返す。"""
    event = AnalyzableArticleCreatedEvent.from_input(RECEIVED_STRINGS)
    assert event.event_id == TYPED_VALUES["event_id"]
    assert event.occurred_at == TYPED_VALUES["occurred_at"]
    assert event.payload == AnalyzableArticleCreated(**TYPED_VALUES["payload"])


def test_from_input_accepts_typed_values():
    """送信元のUUID・日時オブジェクトも受け取り、その値とpayloadを保持する。"""
    event = AnalyzableArticleCreatedEvent.from_input(TYPED_VALUES)
    assert event.event_id == TYPED_VALUES["event_id"]
    assert event.occurred_at == TYPED_VALUES["occurred_at"]
    assert event.payload == AnalyzableArticleCreated(**TYPED_VALUES["payload"])


def test_rejection_does_not_keep_input_or_validation_error():
    """不正なIDを固定の詳細へ変換し、入力値や元の検証例外を残さない。"""
    data = {**RECEIVED_STRINGS, "event_id": "private-id"}
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(data)
    error = caught.value
    assert error.failure.reason.value == "invalid_envelope"
    assert [(i.field.value, i.code.value) for i in error.failure.issues] == [
        ("event_id", "invalid_value")
    ]
    assert error.__cause__ is None and error.__context__ is None
    assert "private-" not in str(error)
    assert "private-" not in "".join(traceback.format_exception(error))


def test_missing_envelope_field_is_invalid_envelope():
    """イベントIDの欠落を外側の構造不正とし、欠けた項目を示す。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {
                "event_type": "article.analyzable_created",
                "schema_version": 1,
                "occurred_at": "2026-09-12T09:00:00.123456+09:00",
                "payload": {"analyzable_article_id": 3},
            }
        )
    assert caught.value.failure.reason.value == "invalid_envelope"
    assert [(i.field.value, i.code.value) for i in caught.value.failure.issues] == [
        ("event_id", "missing_required_field")
    ]


def test_naive_occurred_at_is_invalid():
    """タイムゾーンのない日時を拒否し、日時の値が不正だと示す。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {**RECEIVED_STRINGS, "occurred_at": "2026-09-12T00:00:00"}
        )
    assert caught.value.failure.reason.value == "invalid_envelope"
    assert [(i.field.value, i.code.value) for i in caught.value.failure.issues] == [
        ("occurred_at", "invalid_value")
    ]


def test_unsupported_event_type():
    """別種別のイベントを拒否し、未対応のイベント種別として示す。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {**RECEIVED_STRINGS, "event_type": "private-type"}
        )
    assert caught.value.failure.reason.value == "unsupported_event_type"
    assert [(i.field.value, i.code.value) for i in caught.value.failure.issues] == [
        ("event_type", "unsupported_event_type")
    ]


def test_unsupported_schema_version():
    """未対応の版を拒否し、バージョンの不一致として示す。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {**RECEIVED_STRINGS, "schema_version": 2}
        )
    assert caught.value.failure.reason.value == "unsupported_schema_version"
    assert [(i.field.value, i.code.value) for i in caught.value.failure.issues] == [
        ("schema_version", "unsupported_schema_version")
    ]


def test_payload_id_violation_is_invalid_payload():
    """正整数でないanalyzable_article_idをpayload不正とし、対象のID項目を示す。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {
                **RECEIVED_STRINGS,
                "payload": {"analyzable_article_id": 0},
            }
        )
    assert caught.value.failure.reason.value == "invalid_payload"
    assert [(i.field.value, i.code.value) for i in caught.value.failure.issues] == [
        ("payload.analyzable_article_id", "invalid_value")
    ]


def test_missing_payload_id_is_not_envelope_failure():
    """payload内のID欠落を、外側の構造不正と区別して示す。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {
                **RECEIVED_STRINGS,
                "payload": {},
            }
        )
    assert caught.value.failure.reason.value == "invalid_payload"
    assert [(i.field.value, i.code.value) for i in caught.value.failure.issues] == [
        ("payload.analyzable_article_id", "missing_required_field")
    ]


def test_unknown_keys_are_grouped_without_leaking_names():
    """未知キー名を公開せず親項目へ集約し、同じ詳細を重複させない。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {
                **RECEIVED_STRINGS,
                "private-one": 1,
                "private-two": 2,
                "payload": {
                    "analyzable_article_id": 3,
                    "private-three": 3,
                    "private-four": 4,
                },
            }
        )
    failure = caught.value.failure
    assert failure.reason.value == "invalid_envelope"
    assert {(i.field.value, i.code.value) for i in failure.issues} == {
        ("event", "unknown_field"),
        ("payload", "unknown_field"),
    }
    assert len(failure.issues) == 2
    assert "private-" not in repr(failure)


def test_overlapping_violations_keep_envelope_reason_and_all_details():
    """違反が重なった場合は構造不正を優先し、個々の詳細も保持する。"""
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {
                **RECEIVED_STRINGS,
                "event_type": "private-type",
                "schema_version": 2,
                "occurred_at": "private-time",
                "payload": {"analyzable_article_id": 0},
            }
        )
    assert caught.value.failure.reason.value == "invalid_envelope"
    assert [(i.field.value, i.code.value) for i in caught.value.failure.issues] == [
        ("event_type", "unsupported_event_type"),
        ("schema_version", "unsupported_schema_version"),
        ("occurred_at", "invalid_value"),
        ("payload.analyzable_article_id", "invalid_value"),
    ]


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3", None])
def test_payload_requires_strict_positive_integer(value):
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            {**RECEIVED_STRINGS, "payload": {"analyzable_article_id": value}}
        )
    assert caught.value.failure.reason.value == "invalid_payload"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("event_id", 1),
        ("occurred_at", 1),
    ],
)
def test_envelope_does_not_coerce_invalid_types(field, value):
    with pytest.raises(AnalyzableEventValidationError) as caught:
        AnalyzableArticleCreatedEvent.from_input({**RECEIVED_STRINGS, field: value})
    assert caught.value.failure.reason.value == "invalid_envelope"


def test_event_and_payload_are_immutable_and_serialize_in_utc():
    event = AnalyzableArticleCreatedEvent.from_input(RECEIVED_STRINGS)
    for target, field, value in [
        (event, "schema_version", 2),
        (event.payload, "analyzable_article_id", 4),
    ]:
        with pytest.raises(ValidationError):
            setattr(target, field, value)
    dumped = event.model_dump(mode="json")
    assert dumped["occurred_at"] == "2026-09-12T00:00:00.123456Z"
    assert AnalyzableArticleCreatedEvent.from_input(dumped) == event
