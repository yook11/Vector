"""EmbeddingのJSON解析と、共有イベント契約への接続を確認する。"""

import json
import traceback
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
    AssessedEventValidationError,
)
from app.lambda_handlers.embedding.event import (
    EmbeddingEventInvalidError,
    parse_assessed_in_scope_event,
)

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


def test_parser_restores_typed_event(data):
    """受信本文から、Consumerへ渡す型付きイベントを復元する。"""
    event = parse_assessed_in_scope_event(json.dumps(data))

    assert isinstance(event, ArticleAssessedInScopeEvent)
    assert event.event_id == UUID(int=1)
    assert event.occurred_at == datetime(2026, 9, 7, 3, 0, 0, 123456, tzinfo=UTC)
    assert event.payload == ArticleAssessedInScope(
        curation_id=123, analyzed_article_id=456
    )


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
    """不正JSONは本文を保持せず拒否する。"""
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(body)
    assert caught.value.reason.value == "invalid_json"
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("body", ["null", "[]", "1", "true", '"text"'])
def test_json_root_must_be_an_object(body):
    """JSONのルートがオブジェクトでなければ共有契約の構造不正として返す。"""
    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(body)
    assert caught.value.reason.value == "invalid_envelope"


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "private-id"},
        {"event_type": "private-type"},
        {"schema_version": 2},
        {"payload": {"curation_id": "private-id", "analyzed_article_id": 456}},
    ],
)
def test_shared_rejection_preserves_reason_and_details(data, changes):
    """共有契約の拒否理由と詳細を、受信側の例外へそのまま引き継ぐ。"""
    data.update(changes)
    with pytest.raises(AssessedEventValidationError) as shared:
        ArticleAssessedInScopeEvent.from_input(data)

    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(json.dumps(data))

    assert caught.value.reason.value == shared.value.failure.reason.value
    assert caught.value.issues == shared.value.failure.issues


def test_shared_rejection_does_not_retain_input_or_exception_chain(data):
    """共有契約の検証例外や入力値を、受信側の例外に残さない。"""
    data["payload"]["curation_id"] = "private-input"

    with pytest.raises(EmbeddingEventInvalidError) as caught:
        parse_assessed_in_scope_event(json.dumps(data))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-" not in "".join(traceback.format_exception(caught.value))
