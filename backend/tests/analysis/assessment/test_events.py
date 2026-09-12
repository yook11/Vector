"""共有の構築入口が、型の保証と検証例外の秘匿を担うことを確認する。"""

import traceback
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
    AssessedEventValidationError,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def data():
    return {
        "event_id": str(UUID(int=1)),
        "event_type": "article.assessed_in_scope",
        "schema_version": 1,
        "occurred_at": "2026-09-10T00:00:00Z",
        "payload": {"curation_id": 1, "analyzed_article_id": 2},
    }


def test_from_input_restores_json_strings(data):
    """JSON由来の文字列から、型付きイベントを復元する。"""
    event = ArticleAssessedInScopeEvent.from_input(data)

    assert event.event_id == UUID(int=1)
    assert event.occurred_at == datetime(2026, 9, 10, tzinfo=UTC)
    assert event.payload == ArticleAssessedInScope(curation_id=1, analyzed_article_id=2)


def test_from_input_accepts_native_python_values(data):
    """送信元のUUIDとdatetimeを、そのまま型付きイベントとして受け取る。"""
    data["event_id"] = UUID(int=1)
    data["occurred_at"] = datetime(2026, 9, 10, tzinfo=UTC)

    event = ArticleAssessedInScopeEvent.from_input(data)

    assert event.event_id == data["event_id"]
    assert event.occurred_at == data["occurred_at"]
    assert event.payload == ArticleAssessedInScope(curation_id=1, analyzed_article_id=2)


@pytest.mark.parametrize(
    "changes,reason,field,code",
    [
        ({"event_id": "private-id"}, "invalid_envelope", "event_id", "invalid_value"),
        (
            {"schema_version": True},
            "invalid_envelope",
            "schema_version",
            "invalid_type",
        ),
        (
            {"schema_version": 2},
            "unsupported_schema_version",
            "schema_version",
            "unsupported_schema_version",
        ),
        (
            {"event_type": "private-type"},
            "unsupported_event_type",
            "event_type",
            "unsupported_event_type",
        ),
        (
            {"payload": {"curation_id": "private-value", "analyzed_article_id": 2}},
            "invalid_payload",
            "payload.curation_id",
            "invalid_type",
        ),
        (
            {"private-key": "private-value"},
            "invalid_envelope",
            "event",
            "unknown_field",
        ),
    ],
)
def test_from_input_raises_only_safe_shared_failure(data, changes, reason, field, code):
    """入力やPydantic例外を残さず、共有の理由・項目・コードだけを返す。"""
    data.update(changes)
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    error = caught.value
    assert error.failure.reason.value == reason
    assert [
        (issue.field.value, issue.code.value) for issue in error.failure.issues
    ] == [(field, code)]
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "private-" not in str(error)
    assert "private-" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("data", [None, [], "private-input", 1])
def test_from_input_rejects_non_object_input(data):
    """objectを受け取っても、イベントとして不正なら共有例外へ変換する。"""
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    assert caught.value.failure.reason.value == "invalid_envelope"
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "field", ["event_id", "event_type", "schema_version", "occurred_at", "payload"]
)
def test_missing_envelope_field_is_invalid(data, field):
    """イベントの必須項目が欠けていたら拒否する。"""
    del data[field]
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    assert caught.value.failure.reason.value == "invalid_envelope"


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
def test_contract_rejects_invalid_field_values(data, field, value, reason):
    """契約に合わない項目値を対応する理由で拒否する。"""
    data[field] = value
    with pytest.raises(ValidationError):
        ArticleAssessedInScopeEvent.model_validate(data)
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    assert caught.value.failure.reason.value == reason
    assert "private-" not in str(caught.value)
    assert "private-" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("field", ["curation_id", "analyzed_article_id"])
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", None])
def test_payload_ids_are_strict_positive_integers(data, field, value):
    """payloadのIDは厳密な正の整数だけを受け付ける。"""
    data["payload"][field] = value
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    assert caught.value.failure.reason.value == "invalid_payload"


def test_payload_unknown_field_is_rejected(data):
    """payloadに契約外の項目を許可しない。"""
    data["payload"]["private-field"] = "private-value"
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    assert caught.value.failure.reason.value == "invalid_payload"
    assert "private-" not in str(caught.value)


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
    """複数の契約違反がある場合も、定義された理由の優先順位を守る。"""
    data.update(updates)
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    assert caught.value.failure.reason.value == reason


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
    """検証詳細は入力値を含まない項目とコードへ集約する。"""
    data.update(changes)
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)
    error = caught.value
    assert [
        (issue.field.value, issue.code.value) for issue in error.failure.issues
    ] == expected
    assert "private-" not in str(error)
    assert "private-" not in repr(error.failure.issues)
    assert error.__cause__ is None and error.__context__ is None


def test_event_is_immutable(data):
    """検証後のイベントは書き換えられない。"""
    event = ArticleAssessedInScopeEvent.from_input(data)

    with pytest.raises(ValidationError):
        event.schema_version = 2


def test_payload_is_immutable(data):
    """検証後のpayloadは書き換えられない。"""
    event = ArticleAssessedInScopeEvent.from_input(data)

    with pytest.raises(ValidationError):
        event.payload.curation_id = 9


def test_validation_issue_is_immutable(data):
    """共有の検証詳細は書き換えられない。"""
    data["payload"] = {}
    with pytest.raises(AssessedEventValidationError) as caught:
        ArticleAssessedInScopeEvent.from_input(data)

    with pytest.raises(FrozenInstanceError):
        caught.value.failure.issues[0].field = "private-field"


def test_json_dump_normalizes_time_to_utc_with_fraction(data):
    """JSONの日時はUTCのZ表記に揃え、小数秒を保つ。"""
    data["occurred_at"] = "2026-09-07T12:00:00.123456+09:00"
    event = ArticleAssessedInScopeEvent.from_input(data)

    assert event.model_dump(mode="json")["occurred_at"] == "2026-09-07T03:00:00.123456Z"
    assert event.model_dump()["occurred_at"] == event.occurred_at


def test_json_dump_serializes_uuid_as_string(data):
    """JSONではイベントIDをUUID文字列として出力する。"""
    event = ArticleAssessedInScopeEvent.from_input(data)

    assert event.model_dump(mode="json")["event_id"] == data["event_id"]
