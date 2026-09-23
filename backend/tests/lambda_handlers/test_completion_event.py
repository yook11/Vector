"""補完のJSON解析と取得工程のイベント契約への接続を確認する。"""

import json

import pytest

from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
    IncompleteArticleRecordedEvent,
)
from app.lambda_handlers.sqs.records import SqsRecord
from tests.collection.test_incomplete_article_recorded_event import valid_event


def test_contract_failure_is_not_wrapped(monkeypatch):
    """イベントの検証例外を配送側で再構築せず同じ例外で伝える。"""
    with pytest.raises(IncompleteArticleEventInvalidError) as original:
        IncompleteArticleRecordedEvent.from_input({})

    def reject(_data):
        raise original.value

    monkeypatch.setattr(IncompleteArticleRecordedEvent, "from_input", reject)
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        IncompleteArticleRecordedEvent.from_input(
            SqsRecord(message_id="id", body=json.dumps(valid_event())).parse_json()
        )
    assert caught.value is original.value
