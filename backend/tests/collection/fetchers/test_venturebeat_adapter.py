"""``VentureBeatAdapter`` machinery の per-source 単体テスト (HTTP 非依存, P2)。

P2 で identity ClassVar を廃し ``endpoint_url`` / ``source_name`` を
``__init__`` 注入で受ける。fixture を ``_FakeRssParser`` 経由で食わせ、Adapter
の ``FetchedArticle`` field 内容と、``ArticleFetcher`` 経由の passport 出力を
検証する (identity 固定は test_source_adapter_profiles に集約)。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import feedparser

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.article_limits import ARTICLE_BODY_MIN_LENGTH
from app.collection.domain.observed_article import ObservedArticle, ObservedOrigin
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.fetchers.article_fetcher import ArticleFetcher
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry, normalize_entry
from app.collection.fetchers.venturebeat import VentureBeatAdapter
from app.collection.sources.article_source import ArticleSource
from app.shared.value_objects.source_name import SourceName

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
_ENDPOINT = "https://venturebeat.com/feed"


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


def _adapter_factory(fixture: str) -> Callable[[], VentureBeatAdapter]:
    return lambda: VentureBeatAdapter(
        endpoint_url=_ENDPOINT,
        source_name="VentureBeat",
        parser=_FakeRssParser(fixture),  # type: ignore[arg-type]
    )


async def _collect_fetched(fixture: str) -> list[FetchedArticle]:
    adapter = _adapter_factory(fixture)()
    return [item async for item in adapter.collect()]


def _fetcher(fixture: str) -> ArticleFetcher:
    source = ArticleSource(
        name=SourceName("VentureBeat"),
        endpoint_url=_ENDPOINT,
        observed_origin=ObservedOrigin.feed,
        completion_profile=DEFAULT_PROFILE,
        adapter_factory=_adapter_factory(fixture),
    )
    return ArticleFetcher(source)


async def test_collect_yields_fetched_articles_with_body_from_full_rss() -> None:
    items = await _collect_fetched("venturebeat_rss.xml")

    assert items, "full fixture must yield at least one FetchedArticle"
    assert all(isinstance(item, FetchedArticle) for item in items)
    assert any(
        item.body is not None and len(item.body) >= ARTICLE_BODY_MIN_LENGTH
        for item in items
    )


async def test_collect_yields_short_body_from_teaser_rss() -> None:
    """teaser fixture では body 候補が短く、Ready 昇格条件を満たさない。"""
    items = await _collect_fetched("venturebeat_teaser_rss.xml")

    assert items
    assert all(
        item.body is None or len(item.body) < ARTICLE_BODY_MIN_LENGTH for item in items
    )


async def test_article_fetcher_yields_ready_for_full_rss() -> None:
    """full fixture が最低 1 件の ``AnalyzableArticle`` を yield する (主経路)。"""
    passports = [item async for item in _fetcher("venturebeat_rss.xml").fetch(1)]

    assert any(isinstance(p, AnalyzableArticle) for p in passports)


async def test_article_fetcher_falls_back_to_incomplete_for_teaser_rss() -> None:
    """teaser-only fixture では全 entry が ``ObservedArticle`` に落ちる。"""
    passports = [item async for item in _fetcher("venturebeat_teaser_rss.xml").fetch(1)]

    assert passports
    assert all(isinstance(p, ObservedArticle) for p in passports)
