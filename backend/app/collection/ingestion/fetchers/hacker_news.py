"""Hacker News フェッチャ — Algolia HN Search API クライアント。"""

from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.ingestion.fetchers.source_helpers import (
    get_last_successful_fetch_at,
)
from app.collection.ingestion.persister import (
    ArticleCandidate,
    SourceFetchResult,
    persist_new_articles,
    to_safe_url,
)
from app.config import settings
from app.models.news_source import NewsSource
from app.utils.sanitize import strip_html_tags

HTTP_TIMEOUT = 30.0

logger = structlog.get_logger(__name__)


@dataclass
class HNStory:
    """Hacker News ストーリーの中間表現。"""

    object_id: str
    title: str
    url: str
    points: int
    created_at: datetime
    created_at_i: int
    author: str
    num_comments: int


class HackerNewsFetcher:
    """Algolia HN Search API フェッチャー。"""

    async def _fetch_recent_stories(
        self,
        client: httpx.AsyncClient,
        since_timestamp: int | None = None,
    ) -> list[HNStory]:
        """Algolia HN Search API から最近のストーリーを取得する。

        Args:
            client: HTTP クライアント。
            since_timestamp: Unix タイムスタンプ。これ以降に作成された
                             ストーリーのみ取得する。初回は ``None``
                             （時間フィルタなし）。

        Returns:
            HNStory のリスト（外部 URL を持たないストーリーは除外）。
        """
        params: dict[str, str | int] = {
            "tags": "story",
            "hitsPerPage": settings.hn_hits_per_page,
        }

        numeric_filters = [f"points>{settings.hn_min_points}"]
        if since_timestamp:
            numeric_filters.append(f"created_at_i>{since_timestamp}")
        params["numericFilters"] = ",".join(numeric_filters)

        response = await client.get(
            f"{settings.hn_api_base_url}/search_by_date",
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

    async def fetch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        source: NewsSource,
    ) -> SourceFetchResult:
        """HN のストーリーを取得し ArticleCandidate 経由で永続化する。"""
        result = SourceFetchResult(source_id=source.id)

        # fetch_logs から直近フェッチ時刻を導出
        last_fetched = await get_last_successful_fetch_at(session, source.id)
        since_timestamp: int | None = None
        if last_fetched:
            since_timestamp = int(last_fetched.timestamp())

        try:
            stories = await self._fetch_recent_stories(client, since_timestamp)
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

        # ストーリーを ArticleCandidate に変換（SafeUrl 検証付き）
        candidates: list[ArticleCandidate] = []
        for story in stories:
            safe_url = to_safe_url(story.url)
            if safe_url is None:
                logger.warning(
                    "unsafe_url_skipped",
                    source=source.name,
                    url=story.url[:200],
                )
                result.skipped_count += 1
                continue

            candidates.append(
                ArticleCandidate(
                    url=safe_url,
                    title=strip_html_tags(story.title)[:500],
                    published_at=story.created_at,
                )
            )

        if not candidates:
            return result

        # 永続化を共通ロジックに委譲
        persist_result = await persist_new_articles(session, source, candidates)
        result.new_count = persist_result.new_count
        result.skipped_count += persist_result.skipped_count
        result.new_articles = persist_result.new_articles

        logger.info(
            "hn_fetch_completed",
            source=source.name,
            new=result.new_count,
            skipped=result.skipped_count,
        )
        return result
