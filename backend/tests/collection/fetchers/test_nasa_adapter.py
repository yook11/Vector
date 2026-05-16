"""``NASAAdapter`` の per-source 単体テスト (HTTP 非依存)。

``BaseMultiFeedRssAdapter`` の fan-out 不変条件を NASA 実 subclass 経由で
pin する (Pattern R = ``_build_body`` override 経路を同時に証明)。

固定する不変条件:

- INV-1 dedup: 複数 feed 巡回中に同一 URL が複数 feed に出現しても yield
  URL はユニーク (feed 横断 ``seen_urls`` dedup)
- INV-2 per-feed 耐性: 単一 feed が **任意の** ``ExternalFetchError``
  (recoverable / 非 recoverable 両方) を raise しても他 feed は継続し、
  ``source_feed_fetch_failed`` warning が ``code`` / ``feed`` 付で残る
- INV-3 全 feed 失敗時のみ surface: 全 feed が raise したときだけ最初の
  ``ExternalFetchError`` が ``collect()`` から伝播する
- INV-4 first-error 同一性: 異なる code で全 feed 失敗 → 伝播例外は feed
  順最初のもの、全失敗が ``source_feed_fetch_failed`` ログに残る
- INV-5 0-entry 成功: 全 feed が ``[]`` を返し失敗 0 → 正常終了
- INV-7 subclass ClassVar 期待値
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
    """``skip_url`` のみ指定 ``ExternalFetchError``、他は 1 entry。"""

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
    """全 feed が同一 ``ExternalFetchError`` を raise。"""

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


class _MixedAllFailParser:
    """feed[0] のみ ``FetchOriginServerError``、他は ``FetchResourceNotFoundError``。"""

    def __init__(self, first_feed: str) -> None:
        self._first_feed = first_feed

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        if endpoint_url == self._first_feed:
            raise FetchOriginServerError(status_code=503, reason="service_unavailable")
        raise FetchResourceNotFoundError(status_code=404, reason="not_found")


class _EmptyParser:
    """全 feed が空 list を返す (失敗 0、新着 0)。"""

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,
        parse_mode: str = "text",
        **_: object,
    ) -> list[RssEntry]:
        return []


async def _collect(adapter: NASAAdapter) -> list[FetchedArticle]:
    return [item async for item in adapter.collect()]


async def test_duplicate_urls_across_feeds_are_deduped() -> None:
    adapter = NASAAdapter(parser=_DuplicatingParser())  # type: ignore[arg-type]

    items = await _collect(adapter)

    urls = [i.url for i in items]
    assert urls == ["https://www.nasa.gov/a", "https://www.nasa.gov/b"]


async def test_feed_error_skips_feed_with_warning() -> None:
    skip_url = NASAAdapter.FEEDS[1]
    exc = FetchOriginServerError(status_code=503, reason="service_unavailable")
    adapter = NASAAdapter(parser=_SkipOneFeedParser(skip_url, exc))  # type: ignore[arg-type]

    with capture_logs() as logs:
        items = await _collect(adapter)

    # 6 feed のうち 1 つ skip → 残り 5 feed が 1 entry ずつ
    assert len(items) == len(NASAAdapter.FEEDS) - 1
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
    skip_url = NASAAdapter.FEEDS[1]
    exc = FetchResourceNotFoundError(status_code=404, reason="not_found")
    adapter = NASAAdapter(parser=_SkipOneFeedParser(skip_url, exc))  # type: ignore[arg-type]

    with capture_logs() as logs:
        items = await _collect(adapter)

    # 非 recoverable (404) でも単一 feed 失敗なら source は落ちない
    assert len(items) == len(NASAAdapter.FEEDS) - 1
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
    adapter = NASAAdapter(parser=_AllFailParser(exc))  # type: ignore[arg-type]

    with pytest.raises(FetchResourceNotFoundError):
        await _collect(adapter)


async def test_all_feeds_fail_propagates_the_first_error_code() -> None:
    adapter = NASAAdapter(parser=_MixedAllFailParser(NASAAdapter.FEEDS[0]))  # type: ignore[arg-type]

    with capture_logs() as logs:
        with pytest.raises(FetchOriginServerError):
            await _collect(adapter)

    skips = [
        log for log in logs if log.get("event") == "source_feed_fetch_failed"
    ]
    assert len(skips) == len(NASAAdapter.FEEDS)


async def test_all_feeds_zero_entries_does_not_propagate() -> None:
    adapter = NASAAdapter(parser=_EmptyParser())  # type: ignore[arg-type]

    items = await _collect(adapter)

    assert items == []


def test_subclass_classvars() -> None:
    assert NASAAdapter.NAME == "NASA"
    assert NASAAdapter.ENDPOINT_URL == "https://www.nasa.gov/feed/"
    assert len(NASAAdapter.FEEDS) == 6
    assert NASAAdapter.PARSE_MODE == "text"
