"""CurationのJSON解析と、共有イベント契約への接続を確認する。"""

import json
import traceback
from uuid import UUID

import pytest

from app.collection.events import (
    AnalyzableArticleCreated,
    AnalyzableArticleCreatedEvent,
    AnalyzableEventInvalidError,
)
from app.lambda_handlers.sqs.records import SqsRecord

pytestmark = pytest.mark.unit


@pytest.fixture
def data():
    return {
        "event_id": str(UUID(int=1)),
        "event_type": "article.analyzable_created",
        "schema_version": 1,
        "occurred_at": "2026-09-12T00:00:00.123456Z",
        "payload": {"analyzable_article_id": 3},
    }


def test_parser_returns_whole_event_and_consumer_payload(data):
    """本文からイベント全体を復元し、Consumerが借用する既存payload型を保持する。"""
    event = AnalyzableArticleCreatedEvent.from_input(
        SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
    )
    assert isinstance(event, AnalyzableArticleCreatedEvent)
    assert event.model_dump(mode="json") == data
    assert event.payload == AnalyzableArticleCreated(analyzable_article_id=3)


def test_valid_json_with_wrong_root_uses_event_contract():
    """JSONとして正しい配列は解析不正にせず、共有契約の構造不正として返す。"""
    with pytest.raises(AnalyzableEventInvalidError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            SqsRecord(message_id="id", body="[]").parse_json()
        )
    assert caught.value.invalid.reason == "invalid_envelope"


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "private-id"},
        {"event_type": "private-type"},
        {"schema_version": 2},
        {"payload": {"analyzable_article_id": "private-id"}},
    ],
)
def test_shared_rejection_preserves_reason_and_details_without_exception_chain(
    data, changes
):
    """実共有検証の理由と詳細をそのまま引き継ぎ、元の検証例外を保持しない。"""
    data.update(changes)
    with pytest.raises(AnalyzableEventInvalidError) as shared:
        AnalyzableArticleCreatedEvent.from_input(data)
    with pytest.raises(AnalyzableEventInvalidError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
        )
    error = caught.value
    assert error.invalid.reason.value == shared.value.invalid.reason.value
    assert error.invalid.issues == shared.value.invalid.issues
    assert error.__cause__ is None and error.__context__ is None
    assert "private-" not in str(error)
    assert "private-" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize(
    "original", [RuntimeError("private-failure"), KeyboardInterrupt()]
)
def test_unexpected_failure_and_process_exit_pass_through(data, monkeypatch, original):
    """契約違反以外の障害とプロセス終了を、入力不正へ変換せず同じ例外で伝える。"""

    def fail(_data):
        raise original

    monkeypatch.setattr(AnalyzableArticleCreatedEvent, "from_input", fail)
    with pytest.raises(type(original)) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
        )
    assert caught.value is original


def test_contract_failure_is_not_wrapped(data, monkeypatch):
    """イベントの検証例外を配送側で再構築せず同じ例外で伝える。"""
    with pytest.raises(AnalyzableEventInvalidError) as original:
        AnalyzableArticleCreatedEvent.from_input({})

    def reject(_data):
        raise original.value

    monkeypatch.setattr(AnalyzableArticleCreatedEvent, "from_input", reject)
    with pytest.raises(AnalyzableEventInvalidError) as caught:
        AnalyzableArticleCreatedEvent.from_input(
            SqsRecord(message_id="id", body=json.dumps(data)).parse_json()
        )
    assert caught.value is original.value
