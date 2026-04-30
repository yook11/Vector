"""新ルート Ingestion Service — 1 段で discovered + article を永続化する。

collection-acquisition-redesign Phase 1。新 ``Fetcher`` Protocol が返す
``FetchOutcome`` の Ready を受けて、``discovered_articles`` 行と
``articles`` 行を 1 トランザクションで作る。

責務:

1. ``NewsSource`` の読み込み (無ければ ``SourceNotFoundOutcome``)
2. Fetcher の async iterator を回し、Ready/Failed を分岐
3. Ready → ``DiscoveredArticleRepository.save_many`` + ``ArticleRepository.save`` で
   永続化 (race recovery は両 Repository の既存 on_conflict_do_nothing パターン)
4. ``Article`` Entity (``from_draft``) を組み立てて ``IngestedOutcome`` に詰める
5. ``commit`` まで Service の責務、下流 (Stage C ``extract_content.kiq``) は
   呼び出し側 Task が行う (既存 ``fetch_content`` と対称な責務分担)

旧 ``SourceFetchService`` (URL+title だけ取って fetch_content に渡す 2 段階前提)
とは別系統で、Strangler 移行期間中は並走する。``strategy.NEW_ROUTE_FETCHERS``
に登録されたソースだけが本 Service 経由で取り込まれる。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.extraction.domain import Article
from app.collection.extraction.domain.article import ArticleDraft
from app.collection.extraction.repository import ArticleRepository
from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
)
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FetchedArticle,
    Ready,
)
from app.collection.ingestion.fetchers.protocol import Fetcher
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.models.news_source import NewsSource
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestedOutcome:
    """Fetcher 実行に成功し、0+ 件の Article を永続化した状態。"""

    persisted: list[Article]
    failed_count: int
    skipped_count: int  # discovered/article のいずれかで race 敗北かつ読み戻し不能


@dataclass(frozen=True, slots=True)
class SourceNotFoundOutcome:
    """``source_id`` に対応する ``NewsSource`` が DB に存在しない状態。"""


IngestionOutcome = IngestedOutcome | SourceNotFoundOutcome


class IngestionService:
    """ソース 1 件を新 Protocol Fetcher 経由で 1 段取り込みするユースケース。

    ``PermanentFetchError`` / ``TemporaryFetchError`` は呼び出し側 (Task) に
    伝播する (retry 判断は Task 層の責務)。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        fetcher_factory: Callable[[], Fetcher],
    ) -> None:
        self._session_factory = session_factory
        self._fetcher_factory = fetcher_factory

    async def execute(self, source_id: int) -> IngestionOutcome:
        async with self._session_factory() as session:
            source = await session.get(NewsSource, source_id)
            if source is None:
                logger.warning("ingest_source_not_found", source_id=source_id)
                return SourceNotFoundOutcome()

            fetcher = self._fetcher_factory()

            persisted: list[Article] = []
            failed_count = 0
            skipped_count = 0
            ready_count = 0

            try:
                async for outcome in fetcher.fetch(source):
                    match outcome:
                        case Ready(article=fa, metadata=_m):
                            ready_count += 1
                            article = await self._persist_one(session, source, fa)
                            if article is not None:
                                persisted.append(article)
                            else:
                                skipped_count += 1
                        case Failed(reason=r):
                            failed_count += 1
                            logger.warning(
                                "ingest_source_entry_failed",
                                source_id=source_id,
                                source=source.name,
                                code=r.code,
                                retryable=r.retryable,
                                detail=r.detail,
                            )
            except HostBlockedError as e:
                raise PermanentFetchError(str(e)) from e
            except HostResolutionError as e:
                raise TemporaryFetchError(str(e)) from e

            await session.commit()

        logger.info(
            "ingest_source_completed",
            source_id=source_id,
            source=source.name,
            ready_count=ready_count,
            failed_count=failed_count,
            persisted_count=len(persisted),
            skipped_count=skipped_count,
        )
        return IngestedOutcome(
            persisted=persisted,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

    async def _persist_one(
        self,
        session: AsyncSession,
        source: NewsSource,
        fa: FetchedArticle,
    ) -> Article | None:
        """1 entry を discovered + articles に永続化して Entity を返す。

        Race recovery:

        - discovered_articles: ``save_many`` が空を返したら ``find_by_url`` で読み戻し
        - articles: ``save`` が ``None`` を返したら ``find_by_discovered_article_id``
          で読み戻し

        どちらの読み戻しも失敗した場合のみ ``None`` を返す
        (= skipped、メトリクスでカウント)。
        """
        discovered_id = await self._upsert_discovered(session, source.id, fa)
        if discovered_id is None:
            return None

        article_repo = ArticleRepository(session)
        draft = ArticleDraft(
            title=fa.title,
            body=fa.body,
            published_at=fa.published_at,
        )
        persisted = await article_repo.save(
            draft=draft,
            discovered_article_id=discovered_id,
            source_id=fa.source_id,
            source_url=fa.source_url,
        )
        if persisted is not None:
            return Article.from_draft(
                draft,
                id=persisted.id,
                discovered_article_id=discovered_id,
                created_at=persisted.created_at,
            )

        existing = await article_repo.find_by_discovered_article_id(discovered_id)
        return existing

    async def _upsert_discovered(
        self,
        session: AsyncSession,
        news_source_id: int,
        fa: FetchedArticle,
    ) -> int | None:
        """discovered_articles 行を作って id を返す (既存なら読み戻し)。"""
        candidate = ArticleCandidate(url=fa.source_url, title=fa.title)
        draft = DiscoveredArticleDraft.from_candidate(
            candidate, news_source_id=news_source_id
        )
        repo = DiscoveredArticleRepository(session)
        results = await repo.save_many([draft])
        if results:
            return results[0].id
        existing = await repo.find_by_url(fa.source_url)
        return existing.id if existing else None
