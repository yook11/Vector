"""Hacker News フェッチャ — Algolia HN Search API クライアント。"""

import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.ingestion.domain import ArticleCandidate
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl

# HN フェッチャー固有の運用値。Settings (環境変数経由) には載せない。
# 動的に切り替える運用要件が出た時点でコンストラクタ DI に昇格させる。
HN_API_BASE_URL = "https://hn.algolia.com/api/v1"
HN_MIN_POINTS = 20
HN_HITS_PER_PAGE = 100
HN_SLIDING_WINDOW_SECONDS = 86400  # 24h

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
    """Algolia HN Search API フェッチャー。

    毎サイクル直近 ``HN_SLIDING_WINDOW_SECONDS`` 秒以内に投稿された
    ``points>HN_MIN_POINTS`` のストーリーを全件取得する sliding window 設計。
    increment 用の Redis state は持たず、dedup は repository 層の
    ``ON CONFLICT DO NOTHING`` に委ねる。
    """

    async def _fetch_recent_stories(
        self,
        client: httpx.AsyncClient,
    ) -> list[HNStory]:
        """Algolia HN Search API から sliding window 内のストーリーを取得する。

        Returns:
            HNStory のリスト（外部 URL を持たないストーリーは除外）。
        """
        since = int(time.time()) - HN_SLIDING_WINDOW_SECONDS
        params: dict[str, str | int] = {
            "tags": "story",
            "hitsPerPage": HN_HITS_PER_PAGE,
            "numericFilters": f"points>{HN_MIN_POINTS},created_at_i>{since}",
        }

        response = await client.get(
            f"{HN_API_BASE_URL}/search_by_date",
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
        source: NewsSource,
    ) -> dict[SafeUrl, ArticleCandidate]:
        """HN のストーリーを取得し ``ArticleCandidate`` の dict を返す。

        Raises:
            PermanentFetchError: 403 / 404 / 410 / 451。
            TemporaryFetchError: 429 / 5xx / タイムアウト / ネットワークエラー。
        """
        try:
            stories = await self._fetch_recent_stories(client)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error("hn_http_error", source=source.name, status=status)
            if status in (403, 404, 410, 451):
                raise PermanentFetchError(f"HTTP {status}: {source.name}") from e
            raise TemporaryFetchError(f"HTTP {status}: {source.name}") from e
        except httpx.RequestError as e:
            logger.error("hn_request_error", source=source.name, error=str(e))
            raise TemporaryFetchError(f"request error: {source.name}: {e}") from e

        if not stories:
            logger.info("hn_no_new_stories", source=source.name)
            return {}

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

        return candidates
