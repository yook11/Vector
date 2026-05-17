"""``TechCrunchAdapter`` machinery の per-source 単体テスト (HTTP 非依存, P2)。

P2 で identity ClassVar を廃し ``endpoint_url`` / ``source_name`` を
``__init__`` 注入で受ける。実 RSS fixture を ``_FakeRssParser`` 経由で食わせ、
Adapter が body 候補を持たない ``FetchedArticle`` を yield し、
``ArticleFetcher`` 経由で ``ObservedArticle`` のみが yield されることを検証する
(identity 固定は test_source_adapter_profiles に集約)。
"""

from __future__ import annotations

from pathlib import Path

import feedparser

from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.techcrunch import TechCrunchAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_ENDPOINT = "https://techcrunch.com/feed/"


class _FakeRssParser:
    def __init__(self, fixture_filename: str) -> None:
        self._fixture_filename = fixture_filename

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        path = _FIXTURES_DIR / self._fixture_filename
        feed = feedparser.parse(path.read_bytes())
        return [normalize_entry(raw) for raw in feed.entries]


def _adapter() -> TechCrunchAdapter:
    return TechCrunchAdapter(
        endpoint_url=_ENDPOINT,
        source_name="TechCrunch",
        parser=_FakeRssParser("techcrunch_rss.xml"),  # type: ignore[arg-type]
    )


async def _collect_fetched(adapter: TechCrunchAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_collect_yields_fetched_articles_with_none_body() -> None:
    """TC は RSS body を信用しないため、Adapter は常に ``body=None`` を出す。"""
    items = await _collect_fetched(_adapter())

    assert items, "fixture must yield at least one FetchedArticle"
    assert all(isinstance(item, FetchedArticle) for item in items)
    assert all(item.body is None for item in items)


async def test_collect_propagates_title_and_url_from_entries() -> None:
    """Adapter は entry の title / url を空 str も含めてそのまま渡す責務に閉じる。"""
    items = await _collect_fetched(_adapter())

    assert any(item.title and item.url.startswith("https://") for item in items)


async def test_article_fetcher_yields_incomplete_only() -> None:
    """``ArticleFetcher`` 経路で yield される passport は全 ``ObservedArticle``
    (Ready 経路への昇格は構造的に発生しない)。"""
    source = ArticleSource(
        name=SourceName("TechCrunch"),
        endpoint_url=_ENDPOINT,
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=_adapter,
    )
    fetcher = ArticleFetcher(source)

    passports = [item async for item in fetcher.fetch(source_id=1)]

    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)
