"""``ArticleFetcher`` のユニットテスト (DB / HTTP 非依存)。

``SourceAdapter`` を fake 実装で差し替え、Adapter → builder → passport の
配線が薄い層として正しく動くことを検証する。Adapter 単体の挙動 (RSS parse
や filter) は per-source Adapter テストの責務、本テストは "Adapter が
yield する FetchedArticle を ArticleFetcher が正しく中継する" 一点に絞る。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.collection.article.domain.article import (
    _ARTICLE_BODY_MIN_LENGTH,
    ReadyForArticle,
)
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)

_PUBLISHED = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_VALID_URL = "https://example.com/articles/hello-world"
_VALID_TITLE = "Hello World"
_VALID_BODY = "x" * _ARTICLE_BODY_MIN_LENGTH


class _FakeAdapter:
    NAME = "Fake"
    ENDPOINT_URL = "https://example.test/feed"

    def __init__(self, items: list[FetchedArticle]) -> None:
        self._items = items

    async def collect(self) -> AsyncIterator[FetchedArticle]:
        for item in self._items:
            yield item


async def _collect_fetch(fetcher: ArticleFetcher, source_id: int) -> list:
    return [item async for item in fetcher.fetch(source_id)]


async def test_yields_ready_when_adapter_emits_valid_article() -> None:
    adapter = _FakeAdapter(
        [
            FetchedArticle(
                title=_VALID_TITLE,
                url=_VALID_URL,
                body=_VALID_BODY,
                published_at=_PUBLISHED,
            )
        ]
    )
    fetcher = ArticleFetcher(adapter)

    results = await _collect_fetch(fetcher, source_id=1)

    assert len(results) == 1
    assert isinstance(results[0], ReadyForArticle)


async def test_skips_when_adapter_emits_drop_candidate() -> None:
    """title="" は builder で drop、ArticleFetcher は何も yield しない。"""
    adapter = _FakeAdapter(
        [
            FetchedArticle(
                title="",
                url=_VALID_URL,
                body=_VALID_BODY,
                published_at=_PUBLISHED,
            ),
            FetchedArticle(
                title=_VALID_TITLE,
                url=_VALID_URL,
                body=None,
                published_at=None,
            ),
        ]
    )
    fetcher = ArticleFetcher(adapter)

    results = await _collect_fetch(fetcher, source_id=1)

    assert len(results) == 1
    assert isinstance(results[0], IncompleteArticle)


def test_exposes_adapter_name_and_endpoint_url_as_instance_attrs() -> None:
    adapter = _FakeAdapter([])
    fetcher = ArticleFetcher(adapter)

    assert fetcher.NAME == "Fake"
    assert fetcher.ENDPOINT_URL == "https://example.test/feed"
