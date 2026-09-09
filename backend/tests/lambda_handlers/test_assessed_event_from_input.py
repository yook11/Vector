"""共有の構築入口が、型の保証と検証例外の秘匿を担うことを確認する。"""

import traceback
from datetime import UTC, datetime
from uuid import UUID

import pytest

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


@pytest.mark.parametrize("native", [False, True])
def test_from_input_restores_validated_event(data, native):
    """JSON由来の辞書と送信元のPython値から、同じ型付きイベントを作る。"""
    if native:
        data["event_id"] = UUID(int=1)
        data["occurred_at"] = datetime(2026, 9, 10, tzinfo=UTC)
    event = ArticleAssessedInScopeEvent.from_input(data)
    assert event.event_id == UUID(int=1)
    assert event.payload == ArticleAssessedInScope(curation_id=1, analyzed_article_id=2)
    assert event.model_dump(mode="json")["occurred_at"] == "2026-09-10T00:00:00Z"


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
