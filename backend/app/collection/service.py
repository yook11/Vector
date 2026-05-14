"""Article Acquisition Service — 1 source 分のニュースから品質を担保した記事を獲得する。

外部ニュースを取得して品質を担保した ``articles`` に到達させることが ``collection``
BC の業務目的。本 Service はその中核ユースケースで、source 1 件分の取り込みを担う。
即時獲得 (本文込みで品質を満たす) 経路と補完待ち獲得 (本文 HTML 取得を経て獲得確定
する) 経路の 2 系統を ``match`` で振り分けて永続化する。完成は後段
``ArticleCompletionService`` が担う。

新 2 表構成 (``articles`` / ``pending_html_articles``) を直接駆動する。

Fetcher の ``AsyncIterator[FetchOutcome]`` を回し ``match`` で分岐する:

- ``FetchedEntry(item=ReadyForArticle)`` → 即時獲得経路。
  ``article_repo.save_ready(ready)`` に passport 型を直接渡し、
  ``articles.source_url UNIQUE`` の ``ON CONFLICT DO NOTHING`` で同 tick race /
  既知 URL を吸収する (``None`` 戻りは静かに skip)。caller (``ingest_source`` task)
  は返却された ``article_id`` を ``ExtractionTrigger`` に詰めて
  ``extract_content.kiq`` に chain する。
- ``FetchedEntry(item=IncompleteArticle)`` → 補完待ち獲得経路。
  ``article_repo.exists_by_source_url`` pre-check で feed 再露出を弾き、
  ``pending_html_articles.url`` で投入。下流は cron poller
  (``dispatch_html_fetch_jobs``) が DB 駆動で拾うため、Service / Task は
  pending_id を caller に渡さない (Outcome 純化原則)。
- ``SourceFetchFailed`` → 構造化ログのみ、永続化に流れない。

``commit`` までが Service の責務。``NewsSource`` ORM の lookup は
``IngestSourceArg`` (=task envelope) で済んでいるため本 Service では行わない。
成功側の監査焼付 (``pipeline_events.payload`` への件数 / breakdown 集計) は
中途半端な構造として撤去済。後続で proper な audit subsystem を再導入する予定。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article.domain.article import ReadyForArticle
from app.collection.article.repository import ArticleRepository
from app.collection.errors import PermanentFetchError, TemporaryFetchError
from app.collection.fetchers.outcome import FetchedEntry, SourceFetchFailed
from app.collection.fetchers.protocol import Fetcher
from app.collection.incomplete_article.domain.incomplete_article import (
    IncompleteArticle,
)
from app.collection.incomplete_article.repository import PendingHtmlArticleRepository
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError

logger = structlog.get_logger(__name__)


class ArticleAcquisitionService:
    """1 source 分のニュースを取り込み、品質を担保した記事を獲得する。

    即時獲得可能なものは ``articles`` に直接保存、本文補完を経て獲得するものは
    ``pending_html_articles`` に保管する (後段 ``ArticleCompletionService`` が
    完成させる)。

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

    async def execute(self, source_id: int) -> list[int]:
        async with self._session_factory() as session:
            fetcher = self._fetcher_factory()
            article_repo = ArticleRepository(session)
            pending_repo = PendingHtmlArticleRepository(session)

            persisted_ids: list[int] = []

            try:
                async for outcome in fetcher.fetch(source_id):
                    match outcome:
                        case FetchedEntry(item=ReadyForArticle() as ready):
                            article_id = await article_repo.save_ready(ready)
                            if article_id is None:
                                continue
                            persisted_ids.append(article_id)
                        case FetchedEntry(item=IncompleteArticle() as pending):
                            # pre-check: 既知 URL の HTML fetch 反復を避けるための
                            # コスト節約 (UNIQUE(url) と ON CONFLICT は save 側で
                            # 構造的に担保)
                            if await article_repo.exists_by_source_url(
                                pending.source_url
                            ):
                                continue
                            await pending_repo.save(pending, ready_at=datetime.now(UTC))
                        case SourceFetchFailed(reason=r):
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
            persisted_count=len(persisted_ids),
        )
        return persisted_ids
