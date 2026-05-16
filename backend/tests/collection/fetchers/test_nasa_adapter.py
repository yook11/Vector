"""``NASAAdapter`` の per-source 単体テスト (HTTP 非依存)。

固定する固有不変条件:

- 複数 feed 巡回中に同一 URL が複数 feed に出現しても yield URL はユニーク
  (in-memory ``seen_urls`` dedup の移植証明)
- 1 feed が recoverable な ``ExternalFetchError`` を raise しても他 feed は
  継続し、``nasa_feed_skip`` warning が構造化ログに残る (運用可観測性の移植証明)
- 非 recoverable な ``ExternalFetchError`` は catch せず伝播する
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from structlog.testing import capture_logs

from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.fetchers.nasa import NASAAdapter
from app.collection.fetchers.tools.fetched_article import FetchedArticle
from app.collection.fetchers.tools.rss_parser import RssEntry

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


class _SkipOneFeedParser:
    """``skip_url`` のみ recoverable な ``ExternalFetchError``、他は 1 entry。"""

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


async def _collect(adapter: NASAAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_duplicate_urls_across_feeds_are_deduped() -> None:
    adapter = NASAAdapter(parser=_DuplicatingParser())  # type: ignore[arg-type]

    items = await _collect(adapter)

    urls = [i.url for i in items]
    assert urls == ["https://www.nasa.gov/a", "https://www.nasa.gov/b"]


async def test_recoverable_feed_error_skips_feed_with_warning() -> None:
    skip_url = NASAAdapter.FEEDS[1]
    adapter = NASAAdapter(parser=_SkipOneFeedParser(skip_url))  # type: ignore[arg-type]

    with capture_logs() as logs:
        items = await _collect(adapter)

    # 6 feed のうち 1 つ skip → 残り 5 feed が 1 entry ずつ
    assert len(items) == len(NASAAdapter.FEEDS) - 1
    skips = [
        log
        for log in logs
        if log.get("event") == "nasa_feed_skip" and log.get("feed") == skip_url
    ]
    assert len(skips) == 1
    assert skips[0]["log_level"] == "warning"


async def test_non_recoverable_feed_error_propagates() -> None:
    adapter = NASAAdapter(parser=_PermanentParser())  # type: ignore[arg-type]

    with pytest.raises(FetchResourceNotFoundError):
        await _collect(adapter)
