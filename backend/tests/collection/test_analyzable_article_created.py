"""共通記事完成イベントの最小payload契約を検証する。"""

import pytest
from pydantic import ValidationError

from app.collection.events import AnalyzableArticleCreated


def test_event_has_single_shared_payload():
    """記事IDだけを持つ同じ契約を取得と本文補完で共有する。"""
    event = AnalyzableArticleCreated(analyzable_article_id=42)
    assert (event.EVENT_TYPE, event.SCHEMA_VERSION, event.model_dump()) == (
        "article.analyzable_created",
        1,
        {"analyzable_article_id": 42},
    )
    assert (
        AnalyzableArticleCreated.model_validate_json(event.model_dump_json()) == event
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"analyzable_article_id": 0},
        {"analyzable_article_id": -1},
        {"analyzable_article_id": True},
        {"analyzable_article_id": 1.0},
        {"analyzable_article_id": "1"},
        {"analyzable_article_id": None},
        {"analyzable_article_id": 1, "source_id": 2},
    ],
)
def test_event_rejects_invalid_payload(payload):
    """正の整数以外と余剰項目を入力境界で拒否する。"""
    with pytest.raises(ValidationError):
        AnalyzableArticleCreated.model_validate(payload)


def test_event_is_immutable():
    """検証済み記事IDを後から変更できない。"""
    event = AnalyzableArticleCreated(analyzable_article_id=1)
    with pytest.raises(ValidationError):
        event.analyzable_article_id = 2
