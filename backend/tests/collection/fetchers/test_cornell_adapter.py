"""Cornell Chronicle 取得経路 (``multi_feed_rss`` 共通処理 + Cornell config)。

P2-D で Cornell は ``CornellChronicleSource.collect`` が ``multi_feed_rss``
共通処理に Cornell 固有 config (``CORNELL_FEEDS`` / ``parse_mode="bytes"`` /
body_builder 注入なし = Pattern H) を渡す形になった。純 thin config
(body_builder 既定 = body なし) 経路を直接 pin する。

固定する不変条件:

- INV-1 dedup: 1 記事が複数 taxonomy feed に出現しても yield URL は一意
- INV-2 per-feed 耐性: 単一 feed が **任意の** ``ExternalFetchError`` を
  raise しても他 feed は継続し ``source_feed_fetch_failed`` warning が残る
- INV-3 全 feed 失敗時のみ最初の error が伝播する
- INV-5 0-entry 成功: 全 feed が ``[]`` を返し失敗 0 → 正常終了
- INV-6 Pattern H: yield 全 item の ``body`` は ``None`` (body_builder 既定)
- INV-7 Cornell config: ``CORNELL_FEEDS`` は 6 taxonomy feed
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from structlog.testing import capture_logs

from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.multi_feed_rss import multi_feed_rss
from app.collection.source_fetch.tools.rss_parser import RssEntry
from app.collection.sources.definitions.cornell import CORNELL_FEEDS
from tests.collection.fetchers._fixture_tools import fixture_tools

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
    def __init__(self, skip_url: str, exc: Exception) -> None:
        self._skip_url = skip_url
        self._exc = exc

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        if endpoint_url == self._skip_url:
            raise self._exc
        return [_entry(f"{endpoint_url}#article")]


class _AllFailParser:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        raise self._exc


class _EmptyParser:
    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        return []


def _cornell(parser: object) -> AsyncIterator[FetchedArticle]:
    return multi_feed_rss(
        fixture_tools(rss=parser),
        source_name="Cornell Chronicle",
        feeds=CORNELL_FEEDS,
        parse_mode="bytes",
    )


async def _collect(stream: AsyncIterator[FetchedArticle]) -> list[FetchedArticle]:
    return [item async for item in stream]


async def test_duplicate_urls_across_feeds_are_deduped() -> None:
    items = await _collect(_cornell(_DuplicatingParser()))

    urls = [i.url for i in items]
    assert urls == ["https://news.cornell.edu/x", "https://news.cornell.edu/y"]


async def test_body_is_none_pattern_h() -> None:
    items = await _collect(_cornell(_DuplicatingParser()))

    assert items
    assert all(item.body is None for item in items)


async def test_feed_error_skips_feed_with_warning() -> None:
    skip_url = CORNELL_FEEDS[1]
    exc = FetchOriginServerError(status_code=503, reason="service_unavailable")

    with capture_logs() as logs:
        items = await _collect(_cornell(_SkipOneFeedParser(skip_url, exc)))

    assert len(items) == len(CORNELL_FEEDS) - 1
    skips = [
        log
        for log in logs
        if log.get("event") == "source_feed_fetch_failed"
        and log.get("feed") == skip_url
    ]
    assert len(skips) == 1
    assert skips[0]["log_level"] == "warning"
    assert skips[0]["code"] == "fetch_origin_server_error"


async def test_non_recoverable_single_feed_does_not_propagate() -> None:
    skip_url = CORNELL_FEEDS[1]
    exc = FetchResourceNotFoundError(status_code=404, reason="not_found")

    with capture_logs() as logs:
        items = await _collect(_cornell(_SkipOneFeedParser(skip_url, exc)))

    assert len(items) == len(CORNELL_FEEDS) - 1
    skips = [
        log
        for log in logs
        if log.get("event") == "source_feed_fetch_failed"
        and log.get("feed") == skip_url
    ]
    assert len(skips) == 1
    assert skips[0]["code"] == "fetch_resource_not_found"


async def test_all_feeds_fail_propagates_first_error() -> None:
    exc = FetchResourceNotFoundError(status_code=404, reason="not_found")

    with pytest.raises(FetchResourceNotFoundError):
        await _collect(_cornell(_AllFailParser(exc)))


async def test_all_feeds_zero_entries_does_not_propagate() -> None:
    items = await _collect(_cornell(_EmptyParser()))

    assert items == []


def test_cornell_config_invariants() -> None:
    assert len(CORNELL_FEEDS) == 6
    assert CORNELL_FEEDS[0] == "https://news.cornell.edu/taxonomy/term/24043/feed"
