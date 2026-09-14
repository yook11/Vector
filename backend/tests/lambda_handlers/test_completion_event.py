"""補完のJSON解析と取得工程のイベント契約への接続を確認する。"""

import json

import pytest

from app.collection.article_acquisition.events import (
    IncompleteArticleEventInvalidError,
    IncompleteArticleRecordedEvent,
)
from app.lambda_handlers.completion.event import (
    CompletionMessageJsonInvalidError,
    parse_incomplete_article_recorded_event,
)
from tests.collection.test_incomplete_article_recorded_event import valid_event


def test_invalid_json_does_not_retain_input():
    """JSON構文エラーの本文と元例外を診断用例外へ保持しない。"""
    with pytest.raises(CompletionMessageJsonInvalidError) as caught:
        parse_incomplete_article_recorded_event("private-invalid-json")
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert "private" not in str(caught.value)
    assert "private" not in repr(vars(caught.value))


@pytest.mark.parametrize(
    "body",
    [
        '{"private-key": 1, "private-key": 2}',
        json.dumps(valid_event()).replace(
            '"source_id": 7', '"source_id": 7, "source_id": 8'
        ),
    ],
)
def test_duplicate_json_keys_are_rejected_at_any_depth(body):
    """同名キーを後勝ちにせず、階層を問わずJSON不正にする。"""
    with pytest.raises(CompletionMessageJsonInvalidError) as caught:
        parse_incomplete_article_recorded_event(body)
    assert caught.value.__context__ is None
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_numbers_are_rejected(constant):
    """PythonのJSON拡張で認められる非標準数値を受け入れない。"""
    body = json.dumps(valid_event()).replace(
        '"source_id": 7', f'"source_id": {constant}'
    )
    with pytest.raises(CompletionMessageJsonInvalidError):
        parse_incomplete_article_recorded_event(body)


def test_contract_failure_is_not_wrapped(monkeypatch):
    """イベントの検証例外を配送側で再構築せず同じ例外で伝える。"""
    with pytest.raises(IncompleteArticleEventInvalidError) as original:
        IncompleteArticleRecordedEvent.from_input({})

    def reject(_data):
        raise original.value

    monkeypatch.setattr(IncompleteArticleRecordedEvent, "from_input", reject)
    with pytest.raises(IncompleteArticleEventInvalidError) as caught:
        parse_incomplete_article_recorded_event(json.dumps(valid_event()))
    assert caught.value is original.value
