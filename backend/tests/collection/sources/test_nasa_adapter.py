"""NASA 取得経路 (Source 宣言 + ``MultiFeedRssReader``) の不変条件テスト。

per-feed 失敗隔離 / 全 feed 失敗 raise / 0-entry 成功は ``MultiFeedRssReader``
の責務 (``test_multi_feed_rss_reader``)。本テストは NASA Source 固有の不変条件
— feed 横断 dedup (``select``) / 空 link 素通し (failure-visibility) / NASA config
— を ``fetch_articles`` engine 経由で pin する (``fixture_tools(rss=...)`` で
per-feed parser を注入)。

固定する不変条件:

- INV-1 dedup: 同一 URL が複数 feed に出現しても yield URL は一意
  (``select`` の feed 横断 dedup)
- INV-2 failure-visibility: 空 link entry は dedup 対象外で全 feed 分が素通し、
  converter 層の ``url_empty`` 監査経路 (``ConversionRejection``) を維持する
- INV-3 NASA config: ``NASA_FEEDS`` は 6 feed、``nasa_build_body`` は
  ``content_encoded`` を plain text 化 (Pattern R)
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.sources.definitions.nasa import (
    NASA_FEEDS,
    NASASource,
    nasa_build_body,
)
from tests.collection.sources._fixture_tools import fixture_tools

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _entry(url: str) -> RssEntry:
    return RssEntry(
        link=url,
        title="NASA title",
        guid=url,
        published=_NOW,
        summary=None,
        content_encoded="<p>" + ("body " * 40) + "</p>",
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
            _entry("https://www.nasa.gov/a"),
            _entry("https://www.nasa.gov/b"),
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
    return [item async for item in fetch_articles(NASASource, tools)]


async def test_duplicate_urls_across_feeds_are_deduped() -> None:
    items = await _collect(_DuplicatingParser())

    urls = [i.url for i in items]
    assert urls == ["https://www.nasa.gov/a", "https://www.nasa.gov/b"]


async def test_empty_link_entry_passes_through_for_audit() -> None:
    """空 link entry は drop されず素通し、converter が ``url_empty`` で監査。

    空 link は dedup key にならない (空文字列は URL でない) ため全 feed 分が
    yield される。値欠落の implicit drop は failure-visibility 違反 (converter 層の
    ``ConversionRejection (url_empty)`` 監査経路を逃れる)。
    """
    items = await _collect(_EmptyLinkParser())

    # 6 feed × (空 link 1 + 非空 link 1) = 12 件全部 yield。
    assert len(items) == len(NASA_FEEDS) * 2
    empty_link_count = sum(1 for i in items if i.url == "")
    assert empty_link_count == len(NASA_FEEDS)


def test_nasa_config_invariants() -> None:
    assert len(NASA_FEEDS) == 6
    assert NASA_FEEDS[0] == "https://www.nasa.gov/feed/"
    # Pattern R: content_encoded を plain text 化して本文採用
    entry = _entry("https://www.nasa.gov/x")
    body = nasa_build_body(entry)
    assert body is not None
    assert "<p>" not in body
    assert "body" in body
