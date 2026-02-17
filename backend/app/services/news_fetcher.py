"""News fetcher service — fetches articles from Google News RSS feeds."""

import asyncio
from calendar import timegm
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

import feedparser
import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.models.associations import NewsKeyword
from app.models.keyword import Keyword
from app.models.news import NewsArticle

GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q={keyword}&hl=en-US&gl=US&ceid=US:en"
)
DEFAULT_SOURCE = "Google News"
HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


@dataclass
class FetchResult:
    """Result of a fetch operation across all keywords."""

    new_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


def _build_rss_url(keyword: str) -> str:
    """Build Google News RSS search URL for a keyword."""
    return GOOGLE_NEWS_RSS_URL.format(keyword=quote(keyword))


async def _fetch_rss_feed(
    client: httpx.AsyncClient, url: str
) -> feedparser.FeedParserDict | None:
    """Fetch and parse an RSS feed. Returns None on failure."""
    try:
        response = await client.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        feed = await asyncio.to_thread(feedparser.parse, response.text)
        return feed
    except httpx.HTTPStatusError as e:
        logger.error("rss_fetch_http_error", url=url, status=e.response.status_code)
        return None
    except httpx.RequestError as e:
        logger.error("rss_fetch_request_error", url=url, error=str(e))
        return None


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


async def _get_existing_urls(
    session: AsyncSession, urls: list[str]
) -> set[str]:
    """Check which URLs already exist in the database."""
    if not urls:
        return set()
    stmt = select(NewsArticle.url).where(NewsArticle.url.in_(urls))
    result = await session.execute(stmt)
    return {row[0] for row in result.all()}


async def _link_keyword_to_article(
    session: AsyncSession, article_id: int, keyword_id: int
) -> bool:
    """Create a NewsKeyword link if it doesn't already exist.

    Returns True if a new link was created.
    """
    existing = await session.execute(
        select(NewsKeyword).where(
            NewsKeyword.news_article_id == article_id,
            NewsKeyword.keyword_id == keyword_id,
        )
    )
    if existing.scalar_one_or_none():
        return False
    link = NewsKeyword(news_article_id=article_id, keyword_id=keyword_id)
    session.add(link)
    return True


async def fetch_news_for_keywords(
    session: AsyncSession,
    keywords: list[Keyword],
) -> FetchResult:
    """Fetch news articles for given keywords from Google News RSS.

    Args:
        session: Database session.
        keywords: List of active Keyword model instances.

    Returns:
        FetchResult with counts and any error messages.
    """
    result = FetchResult()

    if not keywords:
        logger.info("fetch_skipped", reason="no keywords provided")
        return result

    # Phase 1: Fetch all RSS feeds and collect entries
    # Map: url -> (entry_dict, set_of_keyword_ids)
    url_to_entry: dict[str, tuple[dict, set[int]]] = {}

    async with httpx.AsyncClient() as client:
        for kw in keywords:
            rss_url = _build_rss_url(kw.keyword)
            logger.info("fetching_rss", keyword=kw.keyword, url=rss_url)

            feed = await _fetch_rss_feed(client, rss_url)
            if feed is None:
                result.error_count += 1
                result.errors.append(
                    f"Failed to fetch RSS for keyword: {kw.keyword}"
                )
                continue

            if feed.bozo and not feed.entries:
                logger.warning(
                    "rss_parse_warning",
                    keyword=kw.keyword,
                    error=str(feed.bozo_exception),
                )
                result.error_count += 1
                result.errors.append(
                    f"RSS parse error for keyword: {kw.keyword}"
                )
                continue

            for entry in feed.entries:
                entry_url = entry.get("link", "")
                if not entry_url:
                    continue

                if entry_url in url_to_entry:
                    url_to_entry[entry_url][1].add(kw.id)
                else:
                    url_to_entry[entry_url] = (entry, {kw.id})

    # Phase 2: Deduplicate against DB
    all_urls = list(url_to_entry.keys())
    existing_urls = await _get_existing_urls(session, all_urls)

    # Phase 3: Create new articles and link keywords
    new_articles_count = 0
    max_new = settings.max_articles_per_fetch

    for url, (entry, keyword_ids) in url_to_entry.items():
        if url in existing_urls:
            # Article exists — just ensure keyword links
            stmt = select(NewsArticle).where(NewsArticle.url == url)
            article_result = await session.execute(stmt)
            article = article_result.scalar_one_or_none()
            if article:
                for kid in keyword_ids:
                    await _link_keyword_to_article(session, article.id, kid)
            result.skipped_count += 1
            continue

        if new_articles_count >= max_new:
            logger.info("fetch_limit_reached", max=max_new)
            break

        # Create new article
        title = entry.get("title", "")[:500]
        description = entry.get("summary") or entry.get("description")

        article = NewsArticle(
            title_original=title,
            description_original=description,
            url=url,
            source=DEFAULT_SOURCE,
            published_at=_parse_published_date(entry),
            fetched_at=datetime.now(UTC),
        )
        session.add(article)
        await session.flush()

        for kid in keyword_ids:
            await _link_keyword_to_article(session, article.id, kid)

        new_articles_count += 1

    result.new_count = new_articles_count
    await session.commit()

    logger.info(
        "fetch_completed",
        new=result.new_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
