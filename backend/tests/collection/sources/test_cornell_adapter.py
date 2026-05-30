"""Cornell Chronicle 取得経路 (Source 宣言 + ``MultiFeedRssReader``)。

per-feed 失敗隔離 / 全 feed 失敗 raise / 0-entry 成功は ``MultiFeedRssReader``
の責務 (``test_multi_feed_rss_reader``)。本テストは Cornell Source 固有の不変条件
— feed 横断 dedup (``select``) / Pattern H (body=None, ``map_entry``) / 空 link
素通し (failure-visibility) / Cornell config — を ``fetch_articles`` engine 経由で
pin する。

固定する不変条件:

- INV-1 dedup: 1 記事が複数 taxonomy feed に出現しても yield URL は一意
- INV-2 Pattern H: yield 全 item の ``body`` は ``None`` (``map_entry`` 既定)
- INV-3 failure-visibility: 空 link entry は dedup 対象外で素通し、converter 層の
  ``url_empty`` 監査経路を維持する (Pattern H なので body も None)
- INV-4 Cornell config: ``CORNELL_FEEDS`` は 6 taxonomy feed
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.sources.definitions.cornell import (
    CORNELL_FEEDS,
    CornellChronicleSource,
)
from tests.collection.sources._fixture_tools import fixture_tools

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _entry(url: str) -> RssEntry:
    return RssEntry(
        link=url,
        title="Cornell title",
        guid=url,
        published=_NOW,
        summary="short teaser",
        content_encoded=None,
        tags=(),
        raw_published=None,
        raw_updated=None,
    )


class _DuplicatingParser:
    """全 feed_url で同一 2 entry を返す (feed 間 URL 重複を再現)。"""

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        return [
            _entry("https://news.cornell.edu/x"),
            _entry("https://news.cornell.edu/y"),
        ]


class _EmptyLinkParser:
    """全 feed が「空 link 1 件 + 非空 link 1 件」を返す (failure-visibility 用)。"""

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        return [_entry(""), _entry(f"{endpoint_url}#article")]


async def _collect(parser: object) -> list[FetchedArticle]:
    tools = fixture_tools(rss=parser)
    return [item async for item in fetch_articles(CornellChronicleSource, tools)]


async def test_duplicate_urls_across_feeds_are_deduped() -> None:
    items = await _collect(_DuplicatingParser())

    urls = [i.url for i in items]
    assert urls == ["https://news.cornell.edu/x", "https://news.cornell.edu/y"]


async def test_body_is_none_pattern_h() -> None:
    items = await _collect(_DuplicatingParser())

    assert items
    assert all(item.body is None for item in items)


async def test_empty_link_entry_passes_through_for_audit() -> None:
    """空 link entry は drop されず素通し、Pattern H で ``body`` は ``None``。

    空 link は dedup key にならないため全 feed 分が yield される。値欠落の
    implicit drop は failure-visibility 違反 (converter 層の
    ``AcquisitionConversionRejection (url_empty)`` 監査経路を逃れる)。
    """
    items = await _collect(_EmptyLinkParser())

    # 6 feed × (空 link 1 + 非空 link 1) = 12 件全部 yield。
    assert len(items) == len(CORNELL_FEEDS) * 2
    empty_link_items = [i for i in items if i.url == ""]
    assert len(empty_link_items) == len(CORNELL_FEEDS)
    # Pattern H: 空 link entry でも map_entry は body=None。
    assert all(item.body is None for item in empty_link_items)


def test_cornell_config_invariants() -> None:
    assert len(CORNELL_FEEDS) == 6
    assert CORNELL_FEEDS[0] == "https://news.cornell.edu/taxonomy/term/24043/feed"
