"""Hacker News フェッチャ — Algolia HN Search API クライアント。"""

from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.news_fetcher import SourceFetchResult
from app.collection.source_helpers import get_last_successful_fetch_at
from app.config import settings
from app.models.news_article import NewsArticle
from app.models.news_source import NewsSource
from app.utils.sanitize import is_safe_url, strip_html_tags

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


class HackerNewsClient:
    """Algolia HN Search API クライアント。"""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self.http_client = http_client
        self.base_url = settings.hn_api_base_url

    async def fetch_recent_stories(
        self,
        since_timestamp: int | None = None,
    ) -> list[HNStory]:
        """Algolia HN Search API から最近のストーリーを取得する。

        Args:
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
        """HN のストーリーを取得し news_articles に保存する。

        - URL の一括突合で重複排除する（RSS フェッチャと同パターン）
        - 新規/スキップ件数を含む SourceFetchResult を返す
        """
        result = SourceFetchResult(source_id=source.id)

        # fetch_logs から直近フェッチ時刻を導出
        last_fetched = await get_last_successful_fetch_at(session, source.id)
        since_timestamp: int | None = None
        if last_fetched:
            since_timestamp = int(last_fetched.timestamp())

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

        # 一括重複排除: 既存 URL を確認
        urls = [s.url for s in stories]
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

        # 新規記事を作成
        max_new = settings.max_articles_per_fetch
        new_count = 0

        for story in stories:
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
                original_title=strip_html_tags(story.title)[:500],
                original_url=story.url,
                news_source_id=source.id,
                published_at=story.created_at,
            )

            session.add(article)
            result.new_articles.append(article)
            new_count += 1
            existing_urls.add(story.url)

        result.new_count = new_count
        logger.info(
            "hn_fetch_completed",
            source=source.name,
            new=new_count,
            skipped=result.skipped_count,
        )
        return result
