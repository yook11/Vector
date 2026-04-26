"""ソースフェッチサービス — ソース単位のメタデータ取得ユースケース。

``ContentFetchService`` と対称な配置。Service はビジネス判断 + fetch + 永続化を
編成し、Task (``fetch_source_metadata``) はキュー機構 (retry 判断 / FetchLog 記録 /
下流 dispatch) を担う。

戻り値は ``SourceFetchOutcome`` tagged union (closed set):

- :class:`SourceFetchedOutcome`   — fetcher 実行に成功し、新規 Entity を
  0+ 件永続化した状態。
- :class:`SourceNotFoundOutcome`  — ``source_id`` に対応する ``NewsSource`` が
  DB に存在しない (enqueue 後の手動削除 / 環境取り違え等)。
- :class:`QuotaSkippedOutcome`    — fetcher の ``DAILY_REQUEST_LIMIT`` 超過で
  外部 fetch を行わずスキップした状態。

呼び出し側 (Task) は ``match`` で網羅し、Outcome を taskiq 戻り値に直接渡さず、
Task 内で payload dict を再構築する (security OS-6)。

Service の責務:
  1. NewsSource の読み込み (無ければ ``SourceNotFoundOutcome``)
  2. ``DAILY_REQUEST_LIMIT`` を持つ fetcher のクォータチェック
     (超過時は ``QuotaSkippedOutcome``)
  3. ``fetcher.fetch`` を強化済み HTTP クライアントとともに呼び出し
  4. ``DiscoveredArticleRepository.save_many`` 経由で新規 Entity を永続化
  5. セッションの commit (``SourceFetchedOutcome`` 経路のみ)
  6. ``SourceFetchedOutcome(new_discovered=[...])`` を返却

Service がやらないこと: FetchLog 書き込み / 下流 dispatch / retry 判断。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
    DiscoveredArticleEntity,
)
from app.collection.ingestion.quota import check_daily_quota
from app.collection.ingestion.registry import get_fetcher
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.config import settings
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; Vector/1.0; +https://github.com/yook11/Vector)"
# Connect/read/write/pool を明示。フェッチャーが share する httpx.AsyncClient の
# defense-in-depth (R4): RSS / API 共に 30 秒以内には応答する想定。
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


@dataclass(frozen=True, slots=True)
class SourceFetchedOutcome:
    """fetcher 実行に成功し、新規 Entity を 0+ 件永続化した状態。"""

    new_discovered: list[DiscoveredArticleEntity]


@dataclass(frozen=True, slots=True)
class SourceNotFoundOutcome:
    """``source_id`` に対応する ``NewsSource`` が DB に存在しない状態。"""


@dataclass(frozen=True, slots=True)
class QuotaSkippedOutcome:
    """fetcher の ``DAILY_REQUEST_LIMIT`` 超過で外部 fetch をスキップした状態。"""


SourceFetchOutcome = SourceFetchedOutcome | SourceNotFoundOutcome | QuotaSkippedOutcome


class SourceFetchService:
    """ソース 1 件のメタデータ取得ユースケース。

    ``PermanentFetchError`` / ``TemporaryFetchError`` は呼び出し側 (Task) に
    伝播する (retry 判断は Task 層の責務)。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, source_id: int) -> SourceFetchOutcome:
        async with self._session_factory() as session:
            source = await session.get(NewsSource, source_id)
            if source is None:
                logger.warning(
                    "source_fetch_not_found",
                    source_id=source_id,
                )
                return SourceNotFoundOutcome()

            fetcher = get_fetcher(source)

            daily_limit = getattr(fetcher, "DAILY_REQUEST_LIMIT", None)
            if daily_limit is not None and not await check_daily_quota(
                source.id, daily_limit
            ):
                logger.info(
                    "source_fetch_quota_exceeded",
                    source_id=source_id,
                    source=source.name,
                )
                return QuotaSkippedOutcome()

            async with httpx.AsyncClient(
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=False,
                verify=True,
                timeout=_HTTP_TIMEOUT,
            ) as client:
                candidates = await fetcher.fetch(client, source)

            new_discovered = await self._persist(session, source, candidates)
            await session.commit()

            logger.info(
                "source_fetch_completed",
                source_id=source_id,
                source=source.name,
                candidates_count=len(candidates),
                new_count=len(new_discovered),
            )
            return SourceFetchedOutcome(new_discovered=new_discovered)

    async def _persist(
        self,
        session: AsyncSession,
        source: NewsSource,
        candidates: dict[SafeUrl, ArticleCandidate],
    ) -> list[DiscoveredArticleEntity]:
        """候補 dict を Draft に束ね、save_many で永続化された Entity を返す。

        入力 dict のキー一意性により URL 重複は型レベルで排除されている。
        ``max_articles_per_fetch`` で 1 fetch あたりの取り込み上限をかけ、DB の
        既存 URL との突き合わせは ``save_many`` の ON CONFLICT DO NOTHING で
        構造的に解消する (Repository 側で ``UNIQUE(original_url)`` を利用)。
        """
        if not candidates:
            return []

        max_new = settings.max_articles_per_fetch
        capped = list(candidates.values())[:max_new]
        if len(candidates) > max_new:
            logger.info(
                "source_fetch_limit_reached",
                source=source.name,
                max=max_new,
            )

        drafts = [
            DiscoveredArticleDraft.from_candidate(c, news_source_id=source.id)
            for c in capped
        ]
        repo = DiscoveredArticleRepository(session)
        return await repo.save_many(drafts)
