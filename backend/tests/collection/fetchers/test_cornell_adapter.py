"""``CornellChronicleAdapter`` の per-source 単体テスト (HTTP 非依存)。

固定する固有不変条件:

- 1 記事が複数 taxonomy feed に出現しても yield URL はユニーク
  (in-memory ``seen_urls`` dedup の移植証明)
- 1 feed が recoverable な ``ExternalFetchError`` を raise しても他 feed は
  継続し、``cornell_feed_skip`` warning が構造化ログに残る
- 非 recoverable な ``ExternalFetchError`` は catch せず伝播する
- Pattern H のため ``body`` は ``None``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from structlog.testing import capture_logs

from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.fetchers.cornell import CornellChronicleAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry

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


class _SkipOneFeedParser:
    def __init__(self, skip_url: str) -> None:
        self._skip_url = skip_url

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        if endpoint_url == self._skip_url:
            raise FetchOriginServerError(status_code=503, reason="service_unavailable")
        return [_entry(f"{endpoint_url}#article")]


class _PermanentParser:
    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        raise FetchResourceNotFoundError(status_code=404, reason="not_found")


async def _collect(adapter: CornellChronicleAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_duplicate_urls_across_feeds_are_deduped() -> None:
    adapter = CornellChronicleAdapter(parser=_DuplicatingParser())  # type: ignore[arg-type]

    items = await _collect(adapter)

    urls = [i.url for i in items]
    assert urls == ["https://news.cornell.edu/x", "https://news.cornell.edu/y"]


async def test_body_is_none_pattern_h() -> None:
    adapter = CornellChronicleAdapter(parser=_DuplicatingParser())  # type: ignore[arg-type]

    items = await _collect(adapter)

    assert items
    assert all(item.body is None for item in items)


async def test_recoverable_feed_error_skips_feed_with_warning() -> None:
    skip_url = CornellChronicleAdapter.FEEDS[1]
    adapter = CornellChronicleAdapter(parser=_SkipOneFeedParser(skip_url))  # type: ignore[arg-type]

    with capture_logs() as logs:
        items = await _collect(adapter)

    assert len(items) == len(CornellChronicleAdapter.FEEDS) - 1
    skips = [
        log
        for log in logs
        if log.get("event") == "cornell_feed_skip" and log.get("feed") == skip_url
    ]
    assert len(skips) == 1
    assert skips[0]["log_level"] == "warning"


async def test_non_recoverable_feed_error_propagates() -> None:
    adapter = CornellChronicleAdapter(parser=_PermanentParser())  # type: ignore[arg-type]

    with pytest.raises(FetchResourceNotFoundError):
        await _collect(adapter)
