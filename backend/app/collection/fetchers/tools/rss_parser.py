"""RSS フィード取得・解析の道具 — 旧 ``BaseRssFetcher`` のロジックをコピー新設。

collection-acquisition-redesign Phase 0c。条件付き GET (ETag / Last-Modified)
+ feedparser パースの責務だけを担い、エントリ変換 (旧 ``convert_entry``) は
各 Fetcher 側に委ねる。

旧 ``BaseRssFetcher`` (``app/collection/ingestion/fetchers/rss/base.py``) は
Phase 2a まで温存し物理削除しない (atomic 切替時に同時に消す)。本モジュールは
意図的なロジックコピーであり、運用窓中の二重管理コストは Phase 2a 完了まで
受け入れる (`spec collection-acquisition-redesign-plan.md §PR-0c`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time

import feedparser
import httpx
import structlog

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.fetchers.http_cache import (
    get_http_cache,
    set_http_cache,
)
from app.models.news_source import NewsSource

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RssEntry:
    """正規化された RSS エントリ。

    feedparser の dict 表現を per-source Fetcher が触らずに済むよう、必要な
    フィールドだけを取り出した surface を提供する。``content_encoded`` は
    VentureBeat のように RSS 本文 (``<content:encoded>``) を直接利用する
    ソース向けに拾い上げる。
    """

    link: str
    title: str
    guid: str | None
    published: datetime | None
    summary: str | None
    content_encoded: str | None


def _to_utc(parsed: struct_time | None) -> datetime | None:
    """feedparser の ``*_parsed`` (struct_time, GMT) を UTC datetime に変換する。"""
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def _extract_published(entry: dict) -> datetime | None:
    """``published_parsed`` を優先、無ければ ``updated_parsed`` にフォールバック。"""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return _to_utc(parsed)


def _extract_content_encoded(entry: dict) -> str | None:
    """``<content:encoded>`` を取り出す。

    feedparser は ``entry.content`` (list of dict) にマップする。
    """
    content = entry.get("content")
    if not content:
        return None
    # content は dict の list (各 dict は value/type/language を持つ)
    for item in content:
        value = item.get("value") if isinstance(item, dict) else None
        if value:
            return value
    return None


def _normalize_entry(entry: dict) -> RssEntry:
    raw_link = entry.get("link") or ""
    raw_guid = entry.get("id") or entry.get("guid")
    return RssEntry(
        link=raw_link,
        title=entry.get("title", ""),
        guid=raw_guid[:2048] if raw_guid else None,
        published=_extract_published(entry),
        summary=entry.get("summary") or None,
        content_encoded=_extract_content_encoded(entry),
    )


class RssParser:
    """RSS フィードの取得 + パースに専念する道具。

    1 ソース 1 フィードの前提で ``fetch_and_parse(source)`` を呼ぶと、条件付き
    GET → feedparser → ``RssEntry`` list の変換まで一気通貫で行う。HTTP cache
    (Redis) の読み書きは ``http_cache.py`` ヘルパに委譲する (旧 BaseRssFetcher
    と同じ Redis キーを共有するため、Phase 2a の atomic 切替前後で cache が
    引き継がれる)。

    Raises:
        PermanentFetchError: 403 / 404 / 410 / 451、フィードのパース失敗。
        TemporaryFetchError: 429 / 5xx / タイムアウト / ネットワークエラー。
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    async def fetch_and_parse(self, source: NewsSource) -> list[RssEntry]:
        """ソースの RSS を取得・パースして ``RssEntry`` のリストを返す。

        ``304 Not Modified`` のときは空リスト (新着なし)。フィード自体は到達
        できたがエントリが 1 件もない場合 (例: bozo + entries 空) は
        ``PermanentFetchError`` を raise する (旧 ``BaseRssFetcher`` と同じ判定)。
        """
        cached_etag, cached_last_modified = await get_http_cache(source.id)

        headers: dict[str, str] = {}
        if cached_etag:
            headers["If-None-Match"] = cached_etag
        if cached_last_modified:
            headers["If-Modified-Since"] = cached_last_modified

        try:
            response = await self._http_client.get(
                str(source.endpoint_url), headers=headers, timeout=HTTP_TIMEOUT
            )
            if response.status_code == 304:
                logger.info("feed_not_modified", source=source.name)
                return []
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error("feed_http_error", source=source.name, status=status)
            if status in (403, 404, 410, 451):
                raise PermanentFetchError(f"HTTP {status}: {source.name}") from e
            raise TemporaryFetchError(f"HTTP {status}: {source.name}") from e
        except httpx.RequestError as e:
            logger.error("feed_request_error", source=source.name, error=str(e))
            raise TemporaryFetchError(f"request error: {source.name}: {e}") from e

        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        await set_http_cache(source.id, etag, last_modified)

        feed = await asyncio.to_thread(feedparser.parse, response.text)
        if feed.bozo and not feed.entries:
            logger.warning(
                "feed_parse_error",
                source=source.name,
                error=str(feed.bozo_exception),
            )
            raise PermanentFetchError(
                f"feed parse error: {source.name}: {feed.bozo_exception}"
            )

        return [_normalize_entry(entry) for entry in feed.entries]
