"""新ルート Ingestion Service — Pattern R は 1 段、Pattern H は 2 段で取り込む。

collection-acquisition-redesign Phase 1 + Phase 1b'。新 ``Fetcher`` Protocol
が返す ``FetchOutcome`` を受けて分岐:

- ``ReadyForArticle`` (Pattern R Fetcher 直接 yield) → ``discovered_articles``
  行 + ``articles`` 行を 1 トランザクションで作成
- ``PendingHtmlFetch`` (Pattern H Fetcher yield) → ``discovered_articles`` 行
  だけ作って ``StagedArticle`` を ``extract_html_body.kiq`` に橋渡し
  (Article 作成は per-entry の 2 段目 task が担う)
- ``Failed`` → 構造化ログのみ

責務:

1. Fetcher の async iterator を回し、3 variants を ``match`` で分岐
2. 永続化 (両 Repository の既存 on_conflict_do_nothing パターンで race recovery)
3. ``Article`` Entity (``from_draft``) を組み立てて ``IngestedOutcome`` に詰める
   (Pattern R 経路のみ。Pattern H は 2 段目で Article が作られるので本 Outcome
   には含まれない)
4. ``commit`` まで Service の責務、下流 (Stage C ``extract_content.kiq`` /
   Pattern H ``extract_html_body.kiq``) は呼び出し側 Task が行う

``NewsSource`` ORM の lookup は本 Service では行わない。``source_id`` を kiq
envelope (``IngestSourceArg``) で受け取った Task 側で 1 度だけ DB query 済の
前提 (Fetcher 自身は ``NAME`` / ``ENDPOINT_URL`` ClassVar で自己完結し、
``source_id`` は永続化時の FK 値としてだけ使う)。
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
    PendingHtmlFetch,
    ReadyForArticle,
)
from app.collection.ingestion.fetchers.protocol import Fetcher
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.collection.ingestion.staged import StagedArticle
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError
from app.shared.value_objects.safe_url import SafeUrl

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestedOutcome:
    """Fetcher 実行に成功した状態。

    - ``persisted``: Pattern R 経路で discovered + article 永続化済の Entity
      (本 Service 内で完結)
    - ``staged``: Pattern H 経路で discovered のみ作成し、HTML 抽出 task
      ``extract_html_body`` への投入が必要な ``StagedArticle`` のリスト
      (Article 作成は 2 段目 task が担う)
    - ``failed_count`` / ``skipped_count``: 観測用カウンタ
    """

    persisted: list[Article]
    staged: list[StagedArticle]
    failed_count: int
    skipped_count: int  # discovered/article のいずれかで race 敗北かつ読み戻し不能


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

    async def execute(self, source_id: int) -> IngestedOutcome:
        async with self._session_factory() as session:
            fetcher = self._fetcher_factory()

            persisted: list[Article] = []
            staged: list[StagedArticle] = []
            failed_count = 0
            skipped_count = 0
            ready_count = 0
            pending_count = 0

            try:
                async for outcome in fetcher.fetch(source_id):
                    match outcome:
                        case ReadyForArticle(article=fa, metadata=_m):
                            ready_count += 1
                            article = await self._persist_one(session, source_id, fa)
                            if article is not None:
                                persisted.append(article)
                            else:
                                skipped_count += 1
                        case PendingHtmlFetch() as pending:
                            pending_count += 1
                            discovered_id = await self._upsert_discovered_url(
                                session,
                                source_id,
                                pending.source_url,
                                pending.title,
                            )
                            if discovered_id is None:
                                skipped_count += 1
                                continue
                            staged.append(
                                StagedArticle(
                                    discovered_id=discovered_id, pending=pending
                                )
                            )
                        case Failed(reason=r):
                            failed_count += 1
                            logger.warning(
                                "ingest_source_entry_failed",
                                source_id=source_id,
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
            ready_count=ready_count,
            pending_count=pending_count,
            failed_count=failed_count,
            persisted_count=len(persisted),
            staged_count=len(staged),
            skipped_count=skipped_count,
        )
        return IngestedOutcome(
            persisted=persisted,
            staged=staged,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

    async def _persist_one(
        self,
        session: AsyncSession,
        source_id: int,
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
        discovered_id = await self._upsert_discovered(session, source_id, fa)
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
        source_id: int,
        fa: FetchedArticle,
    ) -> int | None:
        """discovered_articles 行を作って id を返す (既存なら読み戻し)。"""
        return await self._upsert_discovered_url(
            session, source_id, fa.source_url, fa.title
        )

    async def _upsert_discovered_url(
        self,
        session: AsyncSession,
        source_id: int,
        source_url: SafeUrl,
        title: str,
    ) -> int | None:
        """URL + title から discovered_articles 行を作って id を返す。

        Pattern H 経路 (``PendingHtmlFetch``) では ``FetchedArticle`` がまだ
        存在しないため (body 未確定)、URL + title だけで discovered 行を作る
        必要がある。Pattern R 用 ``_upsert_discovered`` も内部でこれを呼ぶ。
        """
        candidate = ArticleCandidate(url=source_url, title=title)
        draft = DiscoveredArticleDraft.from_candidate(
            candidate, news_source_id=source_id
        )
        repo = DiscoveredArticleRepository(session)
        results = await repo.save_many([draft])
        if results:
            return results[0].id
        existing = await repo.find_by_url(source_url)
        return existing.id if existing else None
