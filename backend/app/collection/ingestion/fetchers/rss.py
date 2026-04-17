"""RSS フェッチャー — RSS ソースから記事を取得する。

単一の RSS ソースに対し、条件付き GET でフィードを取得し、
エントリをパースして ArticleCandidate に変換する。
永続化は persister に委譲する。
HTTP キャッシュ（ETag / Last-Modified）は Redis 経由で管理する。
"""

import asyncio
from calendar import timegm
from datetime import UTC, datetime

import feedparser
import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.ingestion.fetchers.http_cache import get_http_cache, set_http_cache
from app.collection.ingestion.persister import (
    ArticleCandidate,
    SourceFetchResult,
    persist_new_articles,
    to_safe_url,
)
from app.models.news_source import NewsSource
from app.utils.sanitize import strip_html_tags

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


def _parse_published_date(entry: dict) -> datetime | None:
    """feedparser エントリから公開日時を抽出する。"""
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct is None:
        return None
    try:
        timestamp = timegm(time_struct)
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _extract_guid(entry: dict) -> str | None:
    """feedparser エントリから一意な識別子を抽出する。

    entry.id（feedparser が <guid> をマップしたもの）を優先し、
    無い場合は entry.link にフォールバックする。
    """
    guid = entry.get("id") or entry.get("guid")
    if guid:
        return guid[:2048]
    link = entry.get("link")
    if link:
        return link[:2048]
    return None


def _extract_full_content(entry: dict) -> str | None:
    """RSS エントリに含まれる全文コンテンツを抽出する（存在すれば）。

    全文配信を示す content:encoded や content フィールドを確認する。
    """
    # feedparser は content:encoded を entry.content に正規化する
    content_list = entry.get("content")
    if content_list and isinstance(content_list, list):
        for c in content_list:
            value = c.get("value", "")
            # ヒューリスティック: 全文は通常 500 文字超
            if len(value) > 500:
                return value
    return None


async def fetch_rss_source(
    client: httpx.AsyncClient,
    session: AsyncSession,
    source: NewsSource,
) -> SourceFetchResult:
    """RSS ソース 1 件を取得・処理する。"""
    result = SourceFetchResult(source_id=source.id)

    # 条件付き GET 用のヘッダを Redis から読み出す
    cached_etag, cached_last_modified = await get_http_cache(source.id)

    headers: dict[str, str] = {}
    if cached_etag:
        headers["If-None-Match"] = cached_etag
    if cached_last_modified:
        headers["If-Modified-Since"] = cached_last_modified

    try:
        response = await client.get(
            str(source.endpoint_url), headers=headers, timeout=HTTP_TIMEOUT
        )

        # 304 Not Modified — 新着なし
        if response.status_code == 304:
            logger.info("feed_not_modified", source=source.name)
            result.not_modified = True
            return result

        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(
            "feed_http_error",
            source=source.name,
            status=e.response.status_code,
        )
        result.success = False
        result.error_message = f"HTTP {e.response.status_code}"
        return result
    except httpx.RequestError as e:
        logger.error("feed_request_error", source=source.name, error=str(e))
        result.success = False
        result.error_message = str(e)
        return result

    # 次回の条件付き GET に備えて ETag / Last-Modified を保持
    result.etag = response.headers.get("ETag")
    result.last_modified = response.headers.get("Last-Modified")

    # 次回のフェッチサイクルのため Redis に永続化
    await set_http_cache(source.id, result.etag, result.last_modified)

    # フィードをパース
    feed = await asyncio.to_thread(feedparser.parse, response.text)
    if feed.bozo and not feed.entries:
        logger.warning(
            "feed_parse_error",
            source=source.name,
            error=str(feed.bozo_exception),
        )
        result.success = False
        result.error_message = f"Parse error: {feed.bozo_exception}"
        return result

    # エントリを ArticleCandidate に変換
    candidates: list[ArticleCandidate] = []
    for entry in feed.entries:
        raw_url = entry.get("link", "") or _extract_guid(entry) or ""
        if not raw_url:
            continue
        safe_url = to_safe_url(raw_url)
        if safe_url is None:
            logger.warning(
                "unsafe_url_skipped",
                source=source.name,
                url=raw_url[:200],
            )
            result.skipped_count += 1
            continue

        candidates.append(
            ArticleCandidate(
                url=safe_url,
                title=strip_html_tags(entry.get("title", ""))[:500],
                description=strip_html_tags(
                    entry.get("summary") or entry.get("description")
                ),
                content=_extract_full_content(entry),
                published_at=_parse_published_date(entry),
            )
        )

    if not candidates:
        return result

    # 永続化を共通ロジックに委譲
    persist_result = await persist_new_articles(session, source, candidates)

    # persist_result の値を result にマージ（HTTP キャッシュ情報は保持）
    result.new_count = persist_result.new_count
    result.skipped_count += persist_result.skipped_count
    result.new_articles = persist_result.new_articles
    return result


class RssFetcher:
    """SourceFetcher Protocol を満たす RSS フェッチャー。"""

    async def fetch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        source: NewsSource,
    ) -> SourceFetchResult:
        """RSS ソースの記事を取得する。"""
        return await fetch_rss_source(client, session, source)
