"""Alpha Vantage News Sentiment API client."""

from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.news_fetcher import SourceFetchResult
from app.collection.source_helpers import get_last_successful_fetch_at
from app.config import settings
from app.models.fetch_log import FetchLog
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.utils.sanitize import is_safe_url, strip_html_tags

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


def _parse_av_time(time_str: str) -> datetime:
    """Parse Alpha Vantage time_published string to UTC datetime.

    Standard format: YYYYMMDDTHHMMSS (15 chars, with seconds).
    Fallback: YYYYMMDDTHHMM (13 chars, without seconds).
    """
    # Use length to disambiguate since strptime may parse %M/%S as 1 digit
    t_pos = time_str.find("T")
    time_part = time_str[t_pos + 1 :] if t_pos >= 0 else ""
    if len(time_part) >= 6:
        return datetime.strptime(time_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    return datetime.strptime(time_str, "%Y%m%dT%H%M").replace(tzinfo=UTC)


class AlphaVantageClient:
    """Alpha Vantage News Sentiment API client."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self.http_client = http_client
        self.base_url = settings.av_api_base_url
        self.api_key = settings.av_api_key.get_secret_value()

    async def _check_daily_quota(
        self, source: NewsSource, session: AsyncSession
    ) -> bool:
        """Return True if daily quota has NOT been exceeded."""
        today_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        stmt = (
            select(sa_func.count())
            .select_from(FetchLog)
            .where(
                FetchLog.source_id == source.id,
                FetchLog.fetched_at >= today_start,
            )
        )
        result = await session.execute(stmt)
        count = result.scalar_one()
        return count < settings.av_max_daily_requests

    async def fetch_and_save_articles(
        self,
        source: NewsSource,
        session: AsyncSession,
    ) -> SourceFetchResult:
        """Fetch AV news articles and save to news_articles.

        - Deduplication via batch URL checks (same pattern as RSS/HN)
        - Returns SourceFetchResult with new/skipped counts
        """
        result = SourceFetchResult(source_id=source.id)

        if not self.api_key:
            logger.info("av_skipped_no_api_key", source=source.name)
            return result

        # Check daily quota
        if not await self._check_daily_quota(source, session):
            logger.warning("av_daily_quota_exceeded", source=source.name)
            result.success = False
            result.error_message = "Daily API quota exceeded"
            return result

        # Derive last fetch time from fetch_logs
        last_fetched = await get_last_successful_fetch_at(session, source.id)

        params: dict[str, str | int] = {
            "function": "NEWS_SENTIMENT",
            "topics": settings.av_topics,
            "sort": "LATEST",
            "limit": settings.av_limit,
            "apikey": self.api_key,
        }
        if last_fetched:
            params["time_from"] = last_fetched.strftime("%Y%m%dT%H%M")

        try:
            response = await self.http_client.get(
                self.base_url, params=params, timeout=HTTP_TIMEOUT
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error(
                "av_http_error",
                source=source.name,
                status=e.response.status_code,
            )
            result.success = False
            result.error_message = f"HTTP {e.response.status_code}"
            return result
        except httpx.RequestError as e:
            logger.error("av_request_error", source=source.name, error=str(e))
            result.success = False
            result.error_message = str(e)
            return result

        data = response.json()

        # AV returns HTTP 200 with {"Information": "..."} on errors
        if "Information" in data:
            logger.error("av_api_error", source=source.name, info=data["Information"])
            result.success = False
            result.error_message = data["Information"][:500]
            return result

        feed = data.get("feed", [])
        if not feed:
            logger.info("av_no_articles", source=source.name)
            return result

        # Build URLs for batch dedup
        articles_data: list[tuple[dict, str]] = []
        for item in feed:
            url = item.get("url", "")
            if not url:
                continue
            articles_data.append((item, url))

        if not articles_data:
            return result

        urls = [u for _, u in articles_data]
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
        now = datetime.now(UTC)

        for item, url in articles_data:
            if url in existing_urls:
                result.skipped_count += 1
                continue

            # --- XSS対策: URLスキーム検証 ---
            # Alpha Vantage APIから取得したURLも外部データであり、信頼できない。
            # javascript: 等の危険なスキームをDB保存前に排除する。
            if not is_safe_url(url):
                logger.warning(
                    "unsafe_url_skipped",
                    source=source.name,
                    url=url[:200],
                )
                result.skipped_count += 1
                continue

            if new_count >= max_new:
                logger.info("av_fetch_limit_reached", source=source.name, max=max_new)
                break

            try:
                published_at = _parse_av_time(item["time_published"])
            except (ValueError, KeyError):
                published_at = now

            article = NewsArticle(
                original_title=strip_html_tags(item.get("title", ""))[:500],
                original_description=strip_html_tags(item.get("summary")),
                original_url=url,
                news_source_id=source.id,
                published_at=published_at,
            )

            session.add(article)
            result.new_articles.append(article)
            new_count += 1
            existing_urls.add(url)

        result.new_count = new_count
        logger.info(
            "av_fetch_completed",
            source=source.name,
            new=new_count,
            skipped=result.skipped_count,
        )
        return result
