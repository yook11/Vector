"""Hacker News fetcher — Algolia HN Search API client."""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.models.news import NewsArticle
from app.models.news_source import NewsSource
from app.services.news_fetcher import SourceFetchResult
from app.utils.sanitize import is_safe_url, strip_html_tags

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


@dataclass
class HNStory:
    """Intermediate representation of a Hacker News story."""

    object_id: str
    title: str
    url: str
    points: int
    created_at: datetime
    created_at_i: int
    author: str
    num_comments: int


class HackerNewsClient:
    """Algolia HN Search API client."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self.http_client = http_client
        self.base_url = settings.hn_api_base_url

    async def fetch_recent_stories(
        self,
        since_timestamp: int | None = None,
    ) -> list[HNStory]:
        """Fetch recent stories from Algolia HN Search API.

        Args:
            since_timestamp: Unix timestamp. Only fetch stories created after this.
                             None for first fetch (no time filter).

        Returns:
            List of HNStory (stories without external URL are excluded).
        """
        params: dict[str, str | int] = {
            "tags": "story",
            "hitsPerPage": settings.hn_hits_per_page,
        }

        numeric_filters = [f"points>{settings.hn_min_points}"]
        if since_timestamp:
            numeric_filters.append(f"created_at_i>{since_timestamp}")
        params["numericFilters"] = ",".join(numeric_filters)

        response = await self.http_client.get(
            f"{self.base_url}/search_by_date",
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        stories: list[HNStory] = []
        for hit in data.get("hits", []):
            if not hit.get("url"):
                continue
            stories.append(
                HNStory(
                    object_id=hit["objectID"],
                    title=hit["title"],
                    url=hit["url"],
                    points=hit.get("points", 0),
                    created_at=datetime.fromisoformat(
                        hit["created_at"].replace("Z", "+00:00")
                    ),
                    created_at_i=hit["created_at_i"],
                    author=hit.get("author", ""),
                    num_comments=hit.get("num_comments", 0),
                )
            )

        return stories

    async def fetch_and_save_stories(
        self,
        source: NewsSource,
        session: AsyncSession,
    ) -> SourceFetchResult:
        """Fetch HN stories and save to news_articles.

        - guid format: "hn:{objectID}"
        - Deduplication via batch guid/url checks (same pattern as RSS fetcher)
        - Returns SourceFetchResult with new/skipped counts
        """
        result = SourceFetchResult(source_id=source.id)

        # Convert last_fetched_at to unix timestamp for API filter
        since_timestamp: int | None = None
        if source.last_fetched_at:
            since_timestamp = int(source.last_fetched_at.timestamp())

        try:
            stories = await self.fetch_recent_stories(since_timestamp)
        except httpx.HTTPStatusError as e:
            logger.error(
                "hn_http_error",
                source=source.name,
                status=e.response.status_code,
            )
            result.success = False
            result.error_message = f"HTTP {e.response.status_code}"
            return result
        except httpx.RequestError as e:
            logger.error("hn_request_error", source=source.name, error=str(e))
            result.success = False
            result.error_message = str(e)
            return result

        if not stories:
            logger.info("hn_no_new_stories", source=source.name)
            return result

        # Batch dedup: check existing guids and urls
        guids = [f"hn:{s.object_id}" for s in stories]
        urls = [s.url for s in stories]
        existing_guids: set[str] = set()
        existing_urls: set[str] = set()

        chunk_size = 500
        for i in range(0, len(guids), chunk_size):
            chunk = guids[i : i + chunk_size]
            stmt = select(NewsArticle.guid).where(NewsArticle.guid.in_(chunk))
            rows = await session.execute(stmt)
            existing_guids.update(row[0] for row in rows.all())

        for i in range(0, len(urls), chunk_size):
            chunk = urls[i : i + chunk_size]
            stmt = select(NewsArticle.url).where(NewsArticle.url.in_(chunk))
            rows = await session.execute(stmt)
            existing_urls.update(row[0] for row in rows.all())

        # Create new articles
        max_new = settings.max_articles_per_fetch
        new_count = 0
        now = datetime.now(UTC)

        for story in stories:
            guid = f"hn:{story.object_id}"

            if guid in existing_guids:
                result.skipped_count += 1
                continue

            if story.url in existing_urls:
                result.skipped_count += 1
                continue

            # --- XSS対策: URLスキーム検証 ---
            # HN APIから取得したURLも外部ユーザーの投稿データであり、信頼できない。
            # javascript: 等の危険なスキームをDB保存前に排除する。
            if not is_safe_url(story.url):
                logger.warning(
                    "unsafe_url_skipped",
                    source=source.name,
                    url=story.url[:200],
                )
                result.skipped_count += 1
                continue

            if new_count >= max_new:
                logger.info("hn_fetch_limit_reached", source=source.name, max=max_new)
                break

            article = NewsArticle(
                title_original=strip_html_tags(story.title)[:500],
                description_original=None,
                url=story.url,
                source=source.name,
                source_id=source.id,
                guid=guid,
                published_at=story.created_at,
                fetched_at=now,
            )

            session.add(article)
            new_count += 1
            existing_guids.add(guid)
            existing_urls.add(story.url)

        result.new_count = new_count
        logger.info(
            "hn_fetch_completed",
            source=source.name,
            new=new_count,
            skipped=result.skipped_count,
        )
        return result
