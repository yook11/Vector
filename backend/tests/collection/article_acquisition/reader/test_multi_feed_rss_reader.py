"""``MultiFeedRssReader`` の per-feed fan-out 不変条件テスト。

fan-out reader は取得 I/O の頑健性のみを担う道具。結合 ``RssEntry`` 列を返し、
**dedup も写像も持たない** (横断 dedup は Source の ``select``、写像は ``map_entry``)。

固定する不変条件:

- INV-1 per-feed 耐性: 単一 feed が **任意の** ``ExternalFetchError`` (recoverable /
  非 recoverable 両方) を raise しても他 feed は継続し、``source_feed_fetch_failed``
  warning が ``code`` / ``feed`` 付で残る
- INV-2 全 feed 失敗時のみ surface: 全 feed が raise したときだけ最初の error が伝播し、
  全失敗が ``source_feed_fetch_failed`` ログに残る
- INV-3 0-entry 成功: 全 feed が ``[]`` を返し失敗 0 → 正常終了 (空 list)
- INV-4 dedup 非所有: 同一 URL が複数 feed に出ても reader は **両方残す** (dedup は
  Source の ``select`` へ移譲済の構造的証拠)
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from app.collection.article_acquisition.reader.multi_feed_rss_reader import (
    MultiFeedRssReader,
)
from app.collection.article_acquisition.reader.rss_reader import RssEntry
from app.collection.external_fetch_errors import (
    FetchOriginServerError,
    FetchResourceNotFoundError,
)

_FEEDS = (
    "https://example.test/a/feed",
    "https://example.test/b/feed",
    "https://example.test/c/feed",
)


def _entry(url: str) -> RssEntry:
    return RssEntry(
        link=url,
        title="title",
        guid=url,
        published=None,
        summary=None,
        content_encoded=None,
        tags=(),
        raw_published=None,
        raw_updated=None,
    )


class _PerFeedParser:
    """各 feed_url で 1 entry (``{endpoint_url}#article``) を返す。"""

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,  # noqa: ARG002
        parse_mode: str = "text",  # noqa: ARG002
        **_: object,
    ) -> list[RssEntry]:
        return [_entry(f"{endpoint_url}#article")]


class _SkipOneFeedParser:
    """``skip_url`` のみ指定 error、他は 1 entry。"""

    def __init__(self, skip_url: str, exc: Exception) -> None:
        self._skip_url = skip_url
        self._exc = exc

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,  # noqa: ARG002
        parse_mode: str = "text",  # noqa: ARG002
        **_: object,
    ) -> list[RssEntry]:
        if endpoint_url == self._skip_url:
            raise self._exc
        return [_entry(f"{endpoint_url}#article")]


class _AllFailParser:
    """feed[0] のみ ``FetchOriginServerError``、他は ``FetchResourceNotFoundError``。"""

    def __init__(self, first_feed: str) -> None:
        self._first_feed = first_feed

    async def fetch(
        self,
        *,
        endpoint_url: str,
        source_name: str,  # noqa: ARG002
        parse_mode: str = "text",  # noqa: ARG002
        **_: object,
    ) -> list[RssEntry]:
        if endpoint_url == self._first_feed:
            raise FetchOriginServerError(status_code=503, reason="service_unavailable")
        raise FetchResourceNotFoundError(status_code=404, reason="not_found")


class _EmptyParser:
    """全 feed が空 list を返す。"""

    async def fetch(
        self,
        *,
        endpoint_url: str,  # noqa: ARG002
        source_name: str,  # noqa: ARG002
        parse_mode: str = "text",  # noqa: ARG002
        **_: object,
    ) -> list[RssEntry]:
        return []


class _DuplicatingParser:
    """全 feed が同一 URL の 1 entry を返す (feed 間 URL 重複)。"""

    async def fetch(
        self,
        *,
        endpoint_url: str,  # noqa: ARG002
        source_name: str,  # noqa: ARG002
        parse_mode: str = "text",  # noqa: ARG002
        **_: object,
    ) -> list[RssEntry]:
        return [_entry("https://example.test/shared")]


async def _fetch(parser: object) -> list[RssEntry]:
    return await MultiFeedRssReader(rss=parser).fetch(  # type: ignore[arg-type]
        source_name="Example", feeds=_FEEDS, parse_mode="text"
    )


async def test_single_feed_failure_skips_feed_with_warning() -> None:
    skip_url = _FEEDS[1]
    exc = FetchOriginServerError(status_code=503, reason="service_unavailable")

    with capture_logs() as logs:
        entries = await _fetch(_SkipOneFeedParser(skip_url, exc))

    assert len(entries) == len(_FEEDS) - 1
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
    skip_url = _FEEDS[1]
    exc = FetchResourceNotFoundError(status_code=404, reason="not_found")

    entries = await _fetch(_SkipOneFeedParser(skip_url, exc))

    # 非 recoverable (404) でも単一 feed 失敗なら reader は落ちない
    assert len(entries) == len(_FEEDS) - 1


async def test_all_feeds_fail_propagates_first_error() -> None:
    with capture_logs() as logs:
        with pytest.raises(FetchOriginServerError):
            await _fetch(_AllFailParser(_FEEDS[0]))

    skips = [log for log in logs if log.get("event") == "source_feed_fetch_failed"]
    assert len(skips) == len(_FEEDS)


async def test_all_feeds_zero_entries_returns_empty_not_raise() -> None:
    entries = await _fetch(_EmptyParser())

    assert entries == []


async def test_duplicate_urls_across_feeds_are_not_deduped() -> None:
    """dedup は reader の責務でない (Source の ``select`` へ移譲)。

    同一 URL を 3 feed が返したら reader は 3 件すべて残す。1 件に潰れたら
    dedup が reader に残存している退行。
    """
    entries = await _fetch(_DuplicatingParser())

    assert [e.link for e in entries] == ["https://example.test/shared"] * len(_FEEDS)
