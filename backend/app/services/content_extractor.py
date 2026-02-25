"""Content extractor service — fetches full article text from URLs."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from newspaper import Article
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.news import NewsArticle

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"
HEADERS = {"User-Agent": USER_AGENT}


@dataclass
class ContentExtractionResult:
    """Result of extracting content from multiple articles."""

    extracted_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


class RobotsCache:
    """Thread-safe robots.txt cache for concurrent access.

    Uses per-domain asyncio.Lock to prevent duplicate fetches
    when multiple coroutines check the same domain concurrently.
    """

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check(self, client: httpx.AsyncClient, url: str) -> bool:
        """Check if the URL is allowed by robots.txt. Returns True if allowed."""
        parsed = urlparse(url)
        domain = parsed.netloc
        robots_url = f"{parsed.scheme}://{domain}/robots.txt"

        async with self._locks[domain]:
            if domain in self._cache:
                rp = self._cache[domain]
                return rp is None or rp.can_fetch(USER_AGENT, url)

            # First access: fetch robots.txt and cache
            try:
                resp = await client.get(robots_url, timeout=10.0)
                if resp.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(resp.text.splitlines())
                    self._cache[domain] = rp
                else:
                    # No robots.txt or error → assume allowed
                    self._cache[domain] = None
            except httpx.HTTPError:
                self._cache[domain] = None

            rp = self._cache[domain]
            return rp is None or rp.can_fetch(USER_AGENT, url)


class DomainRateLimiter:
    """Rate limiter: parallel across domains, serial within a domain.

    - asyncio.Semaphore: caps total concurrent connections (default 10)
    - asyncio.Lock (per domain): serializes requests to the same domain
    - asyncio.sleep: enforces delay between same-domain requests

    Example: nature.com 5 articles + reuters.com 3 + yahoo.com 2

        nature.com:   [A] →1s→ [B] →1s→ [C] →1s→ [D] →1s→ [E]
        reuters.com:  [F] →1s→ [G] →1s→ [H]
        yahoo.com:    [I] →1s→ [J]
        ↑ these 3 domains run in parallel
    """

    def __init__(self, max_concurrent: int, domain_delay: float) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._domain_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._domain_delay = domain_delay

    @asynccontextmanager
    async def acquire(self, url: str) -> AsyncIterator[None]:
        """Acquire rate-limited access for the given URL's domain."""
        domain = urlparse(url).netloc
        async with self._semaphore:
            async with self._domain_locks[domain]:
                yield
                await asyncio.sleep(self._domain_delay)


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


async def extract_content(
    client: httpx.AsyncClient,
    url: str,
    robots_cache: RobotsCache,
) -> str | None:
    """Extract article content from a URL.

    Uses httpx for async HTTP, newspaper4k parser via asyncio.to_thread().
    Returns None if content cannot be extracted.
    """
    # Check robots.txt
    if not await robots_cache.check(client, url):
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


async def _fetch_one(
    article: NewsArticle,
    client: httpx.AsyncClient,
    rate_limiter: DomainRateLimiter,
    robots_cache: RobotsCache,
) -> tuple[NewsArticle, str | None, Exception | None]:
    """Fetch content for a single article via DomainRateLimiter.

    Returns:
        (article, content, error) tuple.
        - Success: (article, "article text...", None)
        - Robots blocked / empty: (article, None, None)
        - Error: (article, None, Exception)

    This function never raises; all errors are returned in the tuple.
    """
    try:
        async with rate_limiter.acquire(article.url):
            content = await extract_content(client, article.url, robots_cache)
            return (article, content, None)
    except Exception as e:
        logger.warning("content_fetch_error", article_id=article.id, error=str(e))
        return (article, None, e)


async def extract_contents(
    session: AsyncSession,
    articles: list[NewsArticle],
) -> ContentExtractionResult:
    """Extract full content for multiple articles and persist to DB.

    Parallelizes HTTP requests across different domains while maintaining
    per-domain rate limiting (serial + delay within each domain).
    DB operations are performed sequentially after all fetches complete.
    """
    result = ContentExtractionResult()

    if not articles:
        logger.info("content_extraction_skipped", reason="no articles provided")
        return result

    rate_limiter = DomainRateLimiter(
        max_concurrent=settings.content_max_concurrent,
        domain_delay=settings.content_domain_delay,
    )
    robots_cache = RobotsCache()

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=HTTP_TIMEOUT,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ) as client:
        # Phase A: Parallel content fetching (no DB operations)
        fetch_results = await asyncio.gather(
            *[
                _fetch_one(article, client, rate_limiter, robots_cache)
                for article in articles
            ],
            return_exceptions=True,
        )

    # Phase B: Sequential DB persistence (HTTP client no longer needed)
    for res in fetch_results:
        # Uncaught exception from gather (should not happen; _fetch_one catches all)
        if isinstance(res, BaseException):
            result.error_count += 1
            result.errors.append(f"Unexpected: {res}")
            logger.error("content_fetch_unexpected_error", error=str(res))
            continue

        article, content, error = res

        if error:
            result.error_count += 1
            result.errors.append(f"Article {article.id}: {error}")
            logger.warning(
                "content_extraction_failed",
                article_id=article.id,
                error=str(error),
            )
            continue

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

    await session.commit()

    logger.info(
        "content_extraction_completed",
        extracted=result.extracted_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
