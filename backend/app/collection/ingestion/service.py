"""ソースフェッチサービス — ソース単位のメタデータ取得ユースケース。

``ContentFetchService`` と対称な配置。Service はビジネス判断 + fetch + 永続化を
編成し、Task (``fetch_source_metadata``) はキュー機構 (retry 判断 / FetchLog 記録 /
下流 dispatch) を担う。

Service の責務:
  1. NewsSource の読み込み (無ければ ``status="not_found"``)
  2. ``DAILY_REQUEST_LIMIT`` を持つ fetcher のクォータチェック
     (超過時は ``status="skipped_quota"``)
  3. ``fetcher.fetch`` を HTTP クライアントとともに呼び出し
  4. ``DiscoveredArticleRepository`` 経由で新規記事を永続化
  5. セッションの commit
  6. ``SourceFetchResult(status="fetched", new_discovered=[...])`` を返却

Service がやらないこと: FetchLog 書き込み / 下流 dispatch / retry 判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.ingestion.domain import ArticleCandidate
from app.collection.ingestion.quota import check_daily_quota
from app.collection.ingestion.registry import get_fetcher
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.config import settings
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"


@dataclass(frozen=True)
class SourceFetchResult:
    """ソースフェッチユースケースの結果。"""

    status: Literal["fetched", "not_found", "skipped_quota"]
    new_discovered: list[DiscoveredArticle] = field(default_factory=list)


class SourceFetchService:
    """ソース 1 件のメタデータ取得ユースケース。

    ``PermanentFetchError`` / ``TemporaryFetchError`` は呼び出し側 (Task) に
    伝播する (retry 判断は Task 層の責務)。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, source_id: int) -> SourceFetchResult:
        async with self._session_factory() as session:
            source = await session.get(NewsSource, source_id)
            if source is None:
                logger.warning(
                    "source_fetch_not_found",
                    source_id=source_id,
                )
                return SourceFetchResult(status="not_found")

            fetcher = get_fetcher(source)

            daily_limit = getattr(fetcher, "DAILY_REQUEST_LIMIT", None)
            if daily_limit is not None:
                if not await check_daily_quota(source.id, daily_limit):
                    logger.info(
                        "source_fetch_quota_exceeded",
                        source_id=source_id,
                        source=source.name,
                    )
                    return SourceFetchResult(status="skipped_quota")

            async with httpx.AsyncClient(headers={"User-Agent": _USER_AGENT}) as client:
                candidates = await fetcher.fetch(client, source)

            new_discovered = await self._persist(session, source, candidates)
            await session.commit()

            return SourceFetchResult(status="fetched", new_discovered=new_discovered)

    async def _persist(
        self,
        session: AsyncSession,
        source: NewsSource,
        candidates: dict[SafeUrl, ArticleCandidate],
    ) -> list[DiscoveredArticle]:
        """候補 dict を DB に永続化する。

        入力 dict のキー一意性により URL 重複は型レベルで排除されている。
        本メソッドは DB 既存 URL との突き合わせと ``max_articles_per_fetch`` の
        上限制御のみを行う。
        """
        if not candidates:
            return []

        repo = DiscoveredArticleRepository(session)
        existing = await repo.fetch_existing_urls(list(candidates))

        new_discovered: list[DiscoveredArticle] = []
        max_new = settings.max_articles_per_fetch
        for url, candidate in candidates.items():
            if url in existing:
                continue
            if len(new_discovered) >= max_new:
                logger.info(
                    "source_fetch_limit_reached", source=source.name, max=max_new
                )
                break
            discovered = DiscoveredArticle(
                original_title=candidate.title,
                original_url=url,
                news_source_id=source.id,
            )
            repo.add(discovered)
            new_discovered.append(discovered)
        return new_discovered
