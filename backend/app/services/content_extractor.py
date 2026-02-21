"""Content extractor service — fetches full article text from URLs."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from newspaper import Article
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsArticle

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
DOMAIN_DELAY = 1.0  # seconds between requests to same domain
USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"


@dataclass
class ContentExtractionResult:
    """Result of extracting content from multiple articles."""

    extracted_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_article_html(html: str, url: str) -> str | None:
    """Parse article content from HTML using newspaper4k (sync, CPU-bound).

    This function is intended to be run via asyncio.to_thread().
    """
    article = Article(url)
    article.html = html
    article.parse()
    text = article.text
    if not text or len(text.strip()) < 50:
        return None
    return text.strip()


async def _check_robots_txt(
    client: httpx.AsyncClient,
    url: str,
    robots_cache: dict[str, RobotFileParser | None],
) -> bool:
    """Check if the URL is allowed by robots.txt. Returns True if allowed."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    if robots_url not in robots_cache:
        try:
            resp = await client.get(robots_url, timeout=10.0)
            if resp.status_code == 200:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
                robots_cache[robots_url] = rp
            else:
                # No robots.txt or error → assume allowed
                robots_cache[robots_url] = None
        except httpx.HTTPError:
            robots_cache[robots_url] = None

    rp = robots_cache[robots_url]
    if rp is None:
        return True
    return rp.can_fetch(USER_AGENT, url)


async def extract_content(
    client: httpx.AsyncClient,
    url: str,
    robots_cache: dict[str, RobotFileParser | None],
) -> str | None:
    """Extract article content from a URL.

    Uses httpx for async HTTP, newspaper4k parser via asyncio.to_thread().
    Returns None if content cannot be extracted.
    """
    # Check robots.txt
    if not await _check_robots_txt(client, url, robots_cache):
        logger.info("content_blocked_by_robots", url=url)
        return None

    try:
        response = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.warning(
            "content_fetch_http_error",
            url=url,
            status=e.response.status_code,
        )
        return None
    except httpx.RequestError as e:
        logger.warning("content_fetch_request_error", url=url, error=str(e))
        return None

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        logger.info("content_not_html", url=url, content_type=content_type)
        return None

    try:
        text = await asyncio.to_thread(_parse_article_html, response.text, url)
    except Exception as e:
        logger.warning("content_parse_error", url=url, error=str(e))
        return None

    return text


async def extract_contents(
    session: AsyncSession,
    articles: list[NewsArticle],
) -> ContentExtractionResult:
    """Extract full content for multiple articles and persist to DB.

    Implements per-domain rate limiting (1 request/second per domain).
    """
    result = ContentExtractionResult()

    if not articles:
        logger.info("content_extraction_skipped", reason="no articles provided")
        return result

    # Group articles by domain for rate limiting
    domain_last_request: dict[str, float] = defaultdict(float)
    robots_cache: dict[str, RobotFileParser | None] = {}

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers) as client:
        for article in articles:
            domain = urlparse(article.url).netloc

            # Per-domain rate limiting
            now = asyncio.get_event_loop().time()
            elapsed = now - domain_last_request[domain]
            if elapsed < DOMAIN_DELAY:
                await asyncio.sleep(DOMAIN_DELAY - elapsed)

            domain_last_request[domain] = asyncio.get_event_loop().time()

            try:
                content = await extract_content(client, article.url, robots_cache)

                article.content_fetched_at = datetime.now(UTC)
                if content is not None:
                    article.content = content
                    result.extracted_count += 1
                    logger.info(
                        "content_extracted",
                        article_id=article.id,
                        length=len(content),
                    )
                else:
                    result.skipped_count += 1
                    logger.info(
                        "content_extraction_empty",
                        article_id=article.id,
                        url=article.url,
                    )

                session.add(article)

            except Exception as e:
                result.error_count += 1
                result.errors.append(f"Article {article.id}: {e}")
                logger.error(
                    "content_extraction_failed",
                    article_id=article.id,
                    error=str(e),
                )

    await session.commit()

    logger.info(
        "content_extraction_completed",
        extracted=result.extracted_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
