"""EmbeddingのJSON解析と、共有イベント契約への接続を確認する。"""

import json
import traceback
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.analysis.assessment.events import (
    ArticleAssessedInScope,
    ArticleAssessedInScopeEvent,
    AssessedEventInvalidError,
)
from app.lambda_handlers.sqs.records import SqsRecord

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
    event = ArticleAssessedInScopeEvent.from_input(
        SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
    )

    assert isinstance(event, ArticleAssessedInScopeEvent)
    assert event.event_id == UUID(int=1)
    assert event.occurred_at == datetime(2026, 9, 7, 3, 0, 0, 123456, tzinfo=UTC)
    assert event.payload == ArticleAssessedInScope(
        curation_id=123, analyzed_article_id=456
    )


@pytest.mark.parametrize("body", ["null", "[]", "1", "true", '"text"'])
def test_json_root_must_be_an_object(body):
    """JSONのルートがオブジェクトでなければ共有契約の構造不正として返す。"""
    with pytest.raises(AssessedEventInvalidError) as caught:
        ArticleAssessedInScopeEvent.from_input(
            SqsRecord(message_id="id", body=body).parse_json()
        )
    assert caught.value.invalid.reason.value == "invalid_envelope"


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
    """工程の検証例外が、受信時も同じ拒否理由と詳細を伝える。"""
    data.update(changes)
    with pytest.raises(AssessedEventInvalidError) as shared:
        ArticleAssessedInScopeEvent.from_input(data)

    with pytest.raises(AssessedEventInvalidError) as caught:
        ArticleAssessedInScopeEvent.from_input(
            SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
        )

    assert caught.value.invalid.reason.value == shared.value.invalid.reason.value
    assert caught.value.invalid.issues == shared.value.invalid.issues


def test_shared_rejection_does_not_retain_input_or_exception_chain(data):
    """工程の検証例外に入力値や元の検証例外を残さない。"""
    data["payload"]["curation_id"] = "private-input"

    with pytest.raises(AssessedEventInvalidError) as caught:
        ArticleAssessedInScopeEvent.from_input(
            SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-" not in "".join(traceback.format_exception(caught.value))


def test_contract_failure_is_not_wrapped(data, monkeypatch):
    """イベントの検証例外を配送側で再構築せず同じ例外で伝える。"""
    with pytest.raises(AssessedEventInvalidError) as original:
        ArticleAssessedInScopeEvent.from_input({})

    def reject(_data):
        raise original.value

    monkeypatch.setattr(ArticleAssessedInScopeEvent, "from_input", reject)
    with pytest.raises(AssessedEventInvalidError) as caught:
        ArticleAssessedInScopeEvent.from_input(
            SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
        )
    assert caught.value is original.value
