"""RSS フェッチャー — RSS ソースから記事を取得・永続化する。

単一の RSS ソースに対し、条件付き GET でフィードを取得し、
エントリをパースして新規記事を DB に保存する。
HTTP キャッシュ（ETag / Last-Modified）は Redis 経由で管理する。
"""

import asyncio
from calendar import timegm
from datetime import UTC, datetime

import feedparser
import httpx
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.http_cache import get_http_cache, set_http_cache
from app.collection.news_fetcher import SourceFetchResult
from app.config import settings
from app.domain.safe_url import SafeUrl
from app.models.news_article import NewsArticle
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


def _to_safe_url(raw: str) -> SafeUrl | None:
    """生文字列を SafeUrl に変換する。不正な URL は None を返す。"""
    try:
        return SafeUrl(raw)
    except (ValueError, ValidationError):
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

    # エントリの URL を SafeUrl に変換し、不正な URL は除外
    entry_urls: list[tuple[dict, SafeUrl]] = []
    for entry in feed.entries:
        raw_url = entry.get("link", "") or _extract_guid(entry) or ""
        if not raw_url:
            continue
        safe_url = _to_safe_url(raw_url)
        if safe_url is None:
            logger.warning(
                "unsafe_url_skipped",
                source=source.name,
                url=raw_url[:200],
            )
            result.skipped_count += 1
            continue
        entry_urls.append((entry, safe_url))

    if not entry_urls:
        return result

    # 一括重複排除: 既存 URL を確認
    urls = [u for _, u in entry_urls]
    existing_urls: set[SafeUrl] = set()
    chunk_size = 500
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        stmt = select(NewsArticle.original_url).where(
            NewsArticle.original_url.in_(chunk)
        )
        rows = await session.execute(stmt)
        existing_urls.update(row[0] for row in rows.all())

    # 新規記事を作成
    max_new = settings.max_articles_per_fetch
    new_count = 0

    for entry, article_url in entry_urls:
        if article_url in existing_urls:
            result.skipped_count += 1
            continue

        if new_count >= max_new:
            logger.info("source_fetch_limit_reached", source=source.name, max=max_new)
            break

        title = strip_html_tags(entry.get("title", ""))[:500]
        description = strip_html_tags(entry.get("summary") or entry.get("description"))
        full_content = _extract_full_content(entry)

        article = NewsArticle(
            original_title=title,
            original_description=description,
            original_url=article_url,
            news_source_id=source.id,
            published_at=_parse_published_date(entry),
        )

        # RSS が全文を提供している場合はそのまま保存する
        if full_content:
            truncated = full_content[: settings.content_max_length]
            article.original_content = truncated

        session.add(article)
        result.new_articles.append(article)
        new_count += 1
        # 同一フィード内の後続エントリで重複しないよう URL を記録
        existing_urls.add(article_url)

    result.new_count = new_count
    return result
