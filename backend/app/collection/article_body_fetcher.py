"""Article body fetcher — fetches full article text from a URL.

Single-responsibility class: given a URL, return the article body text or
``None`` (when the quality gate rejects it). Permanent and temporary failure
modes are surfaced as exceptions so callers can split business vs. retry
decisions.

Internal implementation (``RobotsCache``, ``httpx`` client lifecycle,
``trafilatura`` parser) is encapsulated here; callers depend only on the
``URL -> body | None`` contract.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
import trafilatura

logger = structlog.get_logger(__name__)

HTTP_TIMEOUT = 30.0
USER_AGENT = "VectorBot/1.0 (+https://github.com/vector-news)"
HEADERS = {"User-Agent": USER_AGENT}


class PermanentFetchError(Exception):
    """Non-retryable fetch failure (403, 404, robots.txt blocked)."""


class TemporaryFetchError(Exception):
    """Retryable fetch failure (5xx, timeout, 429)."""


class _RobotsCache:
    """Thread-safe robots.txt cache for concurrent access.

    Uses per-domain ``asyncio.Lock`` to prevent duplicate fetches when
    multiple coroutines check the same domain concurrently.
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


def _parse_article_html(html: str, url: str) -> str | None:
    """Parse article content from HTML using trafilatura (sync, CPU-bound).

    This function is intended to be run via ``asyncio.to_thread()``.
    """
    text = trafilatura.extract(
        html,
        url=url,
        favor_precision=True,
        include_comments=False,
        include_tables=True,
        deduplicate=True,
    )
    if not text or len(text.strip()) < 50:
        return None
    return text.strip()


class ArticleBodyFetcher:
    """URL → article body text fetcher.

    The caller depends only on ``fetch(url) -> str | None``; the robots cache
    and HTTP client lifecycle are internal.
    """

    def __init__(self) -> None:
        self._robots_cache = _RobotsCache()

    async def fetch(self, url: str) -> str | None:
        """Fetch the article body for the given URL.

        Returns:
            str: Extracted article body text.
            None: Content-Type mismatch or quality gate rejected (permanent).

        Raises:
            PermanentFetchError: robots.txt blocked / 403 / 404 / 410 / 451.
            TemporaryFetchError: 5xx / 429 / timeout / network error.
        """
        async with httpx.AsyncClient(headers=HEADERS, timeout=HTTP_TIMEOUT) as client:
            if not await self._robots_cache.check(client, url):
                raise PermanentFetchError(f"robots.txt blocked: {url}")

            try:
                response = await client.get(
                    url, timeout=HTTP_TIMEOUT, follow_redirects=True
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (403, 404, 410, 451):
                    raise PermanentFetchError(f"HTTP {status}: {url}") from e
                # 429 / 5xx — retryable
                raise TemporaryFetchError(f"HTTP {status}: {url}") from e
            except httpx.RequestError as e:
                # Timeout, DNS, connection error — retryable
                raise TemporaryFetchError(f"request error: {url}: {e}") from e

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
