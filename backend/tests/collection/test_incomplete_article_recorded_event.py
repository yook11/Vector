"""未完成記事の保存イベントの契約を確認する。"""

from uuid import UUID

import pytest

from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
    IncompleteArticleRecorded,
    IncompleteArticleRecordedEvent,
)


def valid_event():
    return {
        "event_id": "00000000-0000-0000-0000-000000000001",
        "event_type": IncompleteArticleRecorded.EVENT_TYPE,
        "schema_version": IncompleteArticleRecorded.SCHEMA_VERSION,
        "occurred_at": "2026-09-14T09:00:00+09:00",
        "payload": {"source_id": 7, "incomplete_article_id": 101},
    }


def test_restores_recorded_incomplete_article():
    """既存のenvelopeから時刻と両IDを持つイベントを復元する。"""
    event = IncompleteArticleRecordedEvent.from_input(valid_event())
    assert event.event_id == UUID(int=1)
    assert event.occurred_at.isoformat() == "2026-09-14T09:00:00+09:00"
    assert event.payload.model_dump() == {"source_id": 7, "incomplete_article_id": 101}


@pytest.mark.parametrize("data", [[], None, 42])
def test_envelope_requires_an_object(data):
    """envelopeの入力をオブジェクトに限定する。"""
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "invalid_envelope"


@pytest.mark.parametrize("field", list(valid_event()))
def test_requires_each_envelope_field(field):
    """envelopeの必須項目を省略できない。"""
    data = valid_event()
    del data[field]
    with pytest.raises(IncompleteArticleEventInvalidError):
        IncompleteArticleRecordedEvent.from_input(data)


@pytest.mark.parametrize("value", ["private-not-uuid", 1])
def test_event_identity_requires_uuid(value):
    """event IDはUUIDとして復元できる値に限定する。"""
    data = valid_event()
    data["event_id"] = value
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "invalid_envelope"
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)
    assert "private" not in repr(vars(caught.value))


def test_unrelated_event_type_is_not_supported():
    """補完以外のイベント種類を受け入れない。"""
    data = valid_event()
    data["event_type"] = "article.analyzable_created"
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "unsupported_event_type"


def test_event_type_requires_string():
    """イベント種類の型を文字列へ変換しない。"""
    data = valid_event()
    data["event_type"] = 1
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert [(issue.field, issue.code) for issue in caught.value.invalid.issues] == [
        ("event_type", "invalid_type")
    ]


def test_undefined_schema_version_is_not_supported():
    """定義されていないschema versionは補完へ渡さない。"""
    data = valid_event()
    data["schema_version"] = IncompleteArticleRecorded.SCHEMA_VERSION + 1
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "unsupported_schema_version"


@pytest.mark.parametrize("value", ["1", True, 1.0])
def test_schema_version_requires_integer_without_coercion(value):
    """schema_versionは整数に限定し、文字列・真偽値・小数へ変換しない。"""
    data = valid_event()
    data["schema_version"] = value
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert [(issue.field, issue.code) for issue in caught.value.invalid.issues] == [
        ("schema_version", "invalid_type")
    ]


def test_occurred_at_requires_timezone():
    """発生日時のタイムゾーンを推測して補わない。"""
    data = valid_event()
    data["occurred_at"] = "2026-09-14T00:00:00"
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert [(issue.field, issue.code) for issue in caught.value.invalid.issues] == [
        ("occurred_at", "invalid_value")
    ]


@pytest.mark.parametrize("value", [123, "private-not-date"])
def test_occurred_at_requires_iso_datetime(value):
    """発生日時はISO日時として復元できる値に限定する。"""
    data = valid_event()
    data["occurred_at"] = value
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "invalid_envelope"
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("value", [[], None])
def test_payload_requires_an_object(value):
    """payloadの外側をオブジェクトに限定する。"""
    data = valid_event()
    data["payload"] = value
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "invalid_envelope"


def test_unknown_envelope_field_does_not_expose_its_name():
    """envelopeの未知項目を拒否し、その名前と値を診断へ持ち込まない。"""
    data = valid_event()
    data["private-unknown-field"] = "private-value"
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert [(issue.field, issue.code) for issue in caught.value.invalid.issues] == [
        ("event", "unknown_field")
    ]
    assert caught.value.__context__ is None


@pytest.mark.parametrize("field", ["source_id", "incomplete_article_id"])
@pytest.mark.parametrize("value", [0, -1, "1", 1.0, True, None, [], {}])
def test_requires_strict_positive_ids(field, value):
    """両IDは型変換せず正の整数に限定する。"""
    data = valid_event()
    data["payload"][field] = value
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "invalid_payload"


@pytest.mark.parametrize("field", ["source_id", "incomplete_article_id"])
def test_requires_each_payload_id(field):
    """payloadの両IDを必須にする。"""
    data = valid_event()
    del data["payload"][field]
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert caught.value.invalid.reason == "invalid_payload"


def test_unknown_payload_field_is_reported_without_its_name():
    """未定義の項目名と値を診断の検証詳細へ持ち込まない。"""
    data = valid_event()
    data["payload"]["private-key"] = "private-value"
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(data)
    assert [(issue.field, issue.code) for issue in caught.value.invalid.issues] == [
        ("payload", "unknown_field")
    ]
    assert caught.value.__context__ is None
