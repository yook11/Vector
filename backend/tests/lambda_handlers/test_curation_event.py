"""CurationのJSON解析と、共有イベント契約への接続を確認する。"""

import json
import traceback
from uuid import UUID

import pytest

from app.collection.events import (
    AnalyzableArticleCreated,
    AnalyzableArticleCreatedEvent,
    AnalyzableEventValidationError,
)
from app.lambda_handlers.curation.event import (
    CurationEventInvalidError,
    CurationEventInvalidReason,
    parse_analyzable_article_created_event,
)

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
    event = parse_analyzable_article_created_event(json.dumps(data))
    assert isinstance(event, AnalyzableArticleCreatedEvent)
    assert event.model_dump(mode="json") == data
    assert event.payload == AnalyzableArticleCreated(analyzable_article_id=3)


@pytest.mark.parametrize(
    "body",
    [
        None,
        b"private-body",
        "",
        '{"private-input":',
        "NaN",
        "Infinity",
        "-Infinity",
        '{"private-key":1,"private-key":2}',
        '{"payload":{"private-key":1,"private-key":2}}',
        '{"payload":{"analyzable_article_id":NaN}}',
    ],
)
def test_invalid_json_is_rejected_without_retaining_body(body):
    """非文字列・壊れたJSON・重複キー・非標準定数を、本文を残さず拒否する。"""
    with pytest.raises(CurationEventInvalidError) as caught:
        parse_analyzable_article_created_event(body)
    error = caught.value
    assert error.CODE == "curation_event_invalid"
    assert error.reason is CurationEventInvalidReason.INVALID_JSON
    assert error.issues == ()
    assert error.__cause__ is None and error.__context__ is None
    assert "private-" not in "".join(traceback.format_exception(error))


def test_deep_json_is_reported_as_invalid_json():
    """実際に深くネストした本文の解析失敗を、入力を持たないJSON不正へ変換する。"""
    depth = 10_000
    body = "[" * depth + "0" + "]" * depth
    with pytest.raises(CurationEventInvalidError) as caught:
        parse_analyzable_article_created_event(body)
    assert caught.value.reason is CurationEventInvalidReason.INVALID_JSON
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_valid_json_with_wrong_root_uses_event_contract():
    """JSONとして正しい配列は解析不正にせず、共有契約の構造不正として返す。"""
    with pytest.raises(CurationEventInvalidError) as caught:
        parse_analyzable_article_created_event("[]")
    assert caught.value.reason is CurationEventInvalidReason.INVALID_ENVELOPE


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
    with pytest.raises(AnalyzableEventValidationError) as shared:
        AnalyzableArticleCreatedEvent.from_input(data)
    with pytest.raises(CurationEventInvalidError) as caught:
        parse_analyzable_article_created_event(json.dumps(data))
    error = caught.value
    assert error.reason.value == shared.value.failure.reason.value
    assert error.issues == shared.value.failure.issues
    assert error.SAFE_ATTRS == ("CODE", "reason", "issues")
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
        parse_analyzable_article_created_event(json.dumps(data))
    assert caught.value is original
