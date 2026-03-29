"""News fetcher service — fetches articles from subscribed news sources."""

import asyncio
import time
from calendar import timegm
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import feedparser
import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource, SourceType
from app.utils.redis_cache import get_http_cache, set_http_cache
from app.utils.sanitize import is_safe_url, strip_html_tags

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


@dataclass
class SourceFetchResult:
    """Result of fetching a single source."""

    source_id: int
    success: bool = True
    new_count: int = 0
    skipped_count: int = 0
    error_message: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass
class FetchResult:
    """Aggregate result across all sources."""

    new_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    source_results: list[SourceFetchResult] = field(default_factory=list)


def _parse_published_date(entry: dict) -> datetime | None:
    """Extract published datetime from a feedparser entry."""
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct is None:
        return None
    try:
        timestamp = timegm(time_struct)
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _extract_guid(entry: dict) -> str | None:
    """Extract a unique identifier from a feedparser entry.

    Prefers entry.id (which feedparser maps from <guid>), falls back to entry.link.
    """
    guid = entry.get("id") or entry.get("guid")
    if guid:
        return guid[:2048]
    link = entry.get("link")
    if link:
        return link[:2048]
    return None


def _extract_full_content(entry: dict) -> str | None:
    """Extract full content from RSS entry if available.

    Checks content:encoded and content fields that indicate full-text RSS feeds.
    """
    # feedparser normalises content:encoded into entry.content
    content_list = entry.get("content")
    if content_list and isinstance(content_list, list):
        for c in content_list:
            value = c.get("value", "")
            # Heuristic: full text is usually > 500 chars
            if len(value) > 500:
                return value
    return None


async def _fetch_rss_source(
    client: httpx.AsyncClient,
    session: AsyncSession,
    source: NewsSource,
) -> SourceFetchResult:
    """Fetch and process a single RSS source."""
    result = SourceFetchResult(source_id=source.id)

    # Read conditional-GET headers from Redis
    cached_etag, cached_last_modified = await get_http_cache(source.id)

    headers: dict[str, str] = {}
    if cached_etag:
        headers["If-None-Match"] = cached_etag
    if cached_last_modified:
        headers["If-Modified-Since"] = cached_last_modified

    try:
        response = await client.get(
            source.endpoint_url, headers=headers, timeout=HTTP_TIMEOUT
        )

        # 304 Not Modified — nothing new
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

    # Capture ETag / Last-Modified for next conditional GET
    result.etag = response.headers.get("ETag")
    result.last_modified = response.headers.get("Last-Modified")

    # Persist to Redis for next fetch cycle
    await set_http_cache(source.id, result.etag, result.last_modified)

    # Parse feed
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

    # Collect entries with URLs for batch dedup
    entry_urls: list[tuple[dict, str]] = []
    for entry in feed.entries:
        url = entry.get("link", "") or _extract_guid(entry) or ""
        if url:
            entry_urls.append((entry, url))

    if not entry_urls:
        return result

    # Batch dedup: check existing URLs
    urls = [u for _, u in entry_urls]
    existing_urls: set[str] = set()
    chunk_size = 500
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        stmt = select(NewsArticle.original_url).where(
            NewsArticle.original_url.in_(chunk)
        )
        rows = await session.execute(stmt)
        # TODO: SafeUrl の __eq__ が str と互換になれば str() 不要
        existing_urls.update(str(row[0]) for row in rows.all())

    # Create new articles
    max_new = settings.max_articles_per_fetch
    new_count = 0

    for entry, article_url in entry_urls:
        if article_url in existing_urls:
            result.skipped_count += 1
            continue

        # --- URL validation: reject articles with unsafe URL schemes ---
        if not is_safe_url(article_url):
            logger.warning(
                "unsafe_url_skipped",
                source=source.name,
                url=article_url[:200],
            )
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

        # If RSS provides full content, store it immediately
        if full_content:
            truncated = full_content[: settings.content_max_length]
            article.original_content = truncated

        session.add(article)
        new_count += 1
        # Track URL so later entries in same feed don't duplicate
        existing_urls.add(article_url)

    result.new_count = new_count
    return result


async def fetch_news_for_sources(
    session: AsyncSession,
    sources: list[NewsSource],
) -> FetchResult:
    """Fetch news articles from the given sources.

    Args:
        session: Database session.
        sources: List of active NewsSource instances to fetch.

    Returns:
        FetchResult with counts, errors, and per-source results.
    """
    result = FetchResult()

    if not sources:
        logger.info("fetch_skipped", reason="no sources provided")
        return result

    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
        }
    ) as client:
        for source in sources:
            logger.info(
                "fetching_source",
                source=source.name,
                type=source.source_type,
            )

            start_time = time.monotonic()

            if source.source_type == SourceType.RSS:
                source_result = await _fetch_rss_source(client, session, source)
            elif source.source_type == SourceType.API:
                # Route to the correct API client based on endpoint_url domain
                domain = urlparse(source.endpoint_url).hostname or ""
                if "algolia.com" in domain:
                    from app.services.hacker_news import HackerNewsClient

                    hn_client = HackerNewsClient(client)
                    source_result = await hn_client.fetch_and_save_stories(
                        source=source, session=session
                    )
                elif "alphavantage.co" in domain:
                    from app.services.alpha_vantage import AlphaVantageClient

                    av_client = AlphaVantageClient(client)
                    source_result = await av_client.fetch_and_save_articles(
                        source=source, session=session
                    )
                else:
                    logger.warning(
                        "unsupported_api_endpoint",
                        source=source.name,
                        endpoint_url=source.endpoint_url,
                    )
                    msg = f"Unsupported API endpoint: {source.endpoint_url}"
                    source_result = SourceFetchResult(
                        source_id=source.id,
                        success=False,
                        error_message=msg,
                    )
            else:
                logger.warning(
                    "unsupported_source_type",
                    source=source.name,
                    type=source.source_type,
                )
                source_result = SourceFetchResult(
                    source_id=source.id,
                    success=False,
                    error_message=f"Unsupported source type: {source.source_type}",
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Record fetch log
            fetch_log = FetchLog(
                source_id=source.id,
                status=(
                    FetchStatus.SUCCESS if source_result.success else FetchStatus.ERROR
                ),
                articles_count=source_result.new_count,
                error_message=source_result.error_message,
                duration_ms=duration_ms,
            )
            session.add(fetch_log)

            result.source_results.append(source_result)
            result.new_count += source_result.new_count
            result.skipped_count += source_result.skipped_count

            if not source_result.success:
                result.error_count += 1
                if source_result.error_message:
                    result.errors.append(
                        f"{source.name}: {source_result.error_message}"
                    )

    await session.commit()

    logger.info(
        "fetch_completed",
        sources=len(sources),
        new=result.new_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
