"""Hacker News フェッチャ — Algolia HN Search API クライアント。"""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.fetchers.hn_fetch_state import (
    get_last_fetched_at,
    set_last_fetched_at,
)
from app.collection.ingestion.persister import (
    ArticleCandidate,
    PersistResult,
    persist_new_articles,
)
from app.config import settings
from app.domain.safe_url import SafeUrl
from app.models.news_source import NewsSource

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
    ) -> PersistResult:
        """HN のストーリーを取得し ArticleCandidate 経由で永続化する。

        Raises:
            PermanentFetchError: 403 / 404 / 410 / 451。
            TemporaryFetchError: 429 / 5xx / タイムアウト / ネットワークエラー。
        """
        # HN 固有の増分取得 state を Redis から読む
        last_fetched = await get_last_fetched_at(source.id)
        since_timestamp: int | None = None
        if last_fetched:
            since_timestamp = int(last_fetched.timestamp())

        try:
            stories = await self._fetch_recent_stories(client, since_timestamp)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error("hn_http_error", source=source.name, status=status)
            if status in (403, 404, 410, 451):
                raise PermanentFetchError(f"HTTP {status}: {source.name}") from e
            raise TemporaryFetchError(f"HTTP {status}: {source.name}") from e
        except httpx.RequestError as e:
            logger.error("hn_request_error", source=source.name, error=str(e))
            raise TemporaryFetchError(f"request error: {source.name}: {e}") from e

        # 成功した時点で次回の増分取得キーを更新
        await set_last_fetched_at(source.id, datetime.now(UTC))

        if not stories:
            logger.info("hn_no_new_stories", source=source.name)
            return PersistResult()

        # ストーリーを ArticleCandidate に変換（SafeUrl 検証・タイトル整形は内部で担保）
        # dict 組み立てにより URL 重複は先勝ちで型レベル排除される
        candidates: dict[SafeUrl, ArticleCandidate] = {}
        for story in stories:
            candidate = ArticleCandidate.from_external(
                raw_url=story.url, raw_title=story.title
            )
            if candidate is None:
                logger.warning(
                    "hn_candidate_rejected",
                    source=source.name,
                    url=story.url[:200],
                )
                continue
            candidates.setdefault(candidate.url, candidate)

        if not candidates:
            return PersistResult()

        result = await persist_new_articles(session, source, candidates)
        logger.info(
            "hn_fetch_completed",
            source=source.name,
            new=len(result.new_discovered),
        )
        return result
