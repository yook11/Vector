"""``ArticleCompletionService`` — 未完成記事を完成形に補完する use case。

``pending_html_articles`` 駆動。Task 層が構築した厚い Ready を ``execute(ready)``
で受け取り、唯一のオーケストレータとして 3 Stage を順に呼ぶ。重複は
``articles.source_url UNIQUE`` が防ぎ、race-loss は read-back せず log + ``None``
で短絡する。

- Stage 1 取得: ``ArticleHtmlAcquirer.acquire`` (never raise の二値) を呼び
  ``AcquiredContent | AcquisitionFailure`` を値で受ける。
- Stage 2 完成: ``ArticleHtmlCompleter.complete`` (純粋 sync アダプタ) に
  ``AcquiredContent`` を渡し ``AnalyzableArticle | CompletionRejection``
  を受ける。
- Stage 3 永続化: ``articles`` INSERT + ``pending_html_articles`` DELETE は同 tx で
  一括 commit。真の DB 異常は例外として伝播。
- 失敗は concern 別に分類し ``ArticleCompletionFailureHandler`` の 2 入口へ委譲:
  Stage 1 (acquisition) は ``handle_acquisition_failure``、Stage 2 (completion)
  は ``handle_completion_rejected`` (状態遷移 + log は handler の責務)。retry は
  DB 駆動で taskiq retry は使わない。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_completion.acquirer import (
    AcquiredContent,
    ArticleHtmlAcquirer,
)
from app.collection.article_completion.acquisition_failure import (
    classify_acquisition_failure,
)
from app.collection.article_completion.completer import ArticleHtmlCompleter
from app.collection.article_completion.completion_failure import (
    CompletionRejection,
)
from app.collection.article_completion.failure_handling import (
    ArticleCompletionFailureHandler,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.domain.analyzable_article import AnalyzableArticle

logger = structlog.get_logger(__name__)


class ArticleCompletionService:
    """Ready 1 件を取得 → 完成 → 永続化する。

    ``execute(ready)`` が単一エントリポイント。Stage 1 (acquire) と Stage 2
    (complete) をそれぞれ二値の collaborator に委譲し、各失敗を concern 別に
    ``ArticleCompletionFailureHandler`` の 2 入口へ委譲する (retry は DB 駆動、
    caller に raise しない)。service を読めば取得 → 完成 → 永続化の流れが分かる。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        acquirer_factory: Callable[[], ArticleHtmlAcquirer] = ArticleHtmlAcquirer,
    ) -> None:
        self._session_factory = session_factory
        self._acquirer_factory = acquirer_factory
        self._completer = ArticleHtmlCompleter()
        self._failure_handler = ArticleCompletionFailureHandler(session_factory)

    async def execute(self, ready: ReadyForArticleCompletion) -> int | None:
        """Ready 1 件を HTML 取得 → promotion → 永続化までの一連を担う。

        precondition (``status='running'``) は ``ReadyForArticleCompletion``
        で保証済。

        Returns:
            ``int`` — 永続化済 ``article_id``。caller は ``curate_content.kiq``
            に chain する。
            ``None`` — lease 衝突 / 状態不整合 / 永続失敗 / 一時失敗 /
            race-loss (静かに exit)。失敗詳細は構造化ログで観測する。
        """
        # Stage 1: 取得（URL → 抽出物 or 取得失敗）。acquire は never raise の二値。
        acquirer = self._acquirer_factory()
        acquired = await acquirer.acquire(ready.source_url.as_safe_url())
        match acquired:
            case AcquiredContent() as content:
                pass
            case _ as failure:  # AcquisitionFailure (transport + content の 5 variant)
                await self._failure_handler.handle_acquisition_failure(
                    ready, classify_acquisition_failure(failure)
                )
                return None

        # Stage 2: 完成（抽出物 → AnalyzableArticle or 構築拒否）。
        built = self._completer.complete(ready, content)
        match built:
            case AnalyzableArticle() as advanced:
                pass
            case CompletionRejection() as rejection:
                await self._failure_handler.handle_completion_rejected(ready, rejection)
                return None
            case _ as unreachable:
                assert_never(unreachable)

        # Stage 3: 永続化。
        canonical_url = ready.source_url
        async with self._session_factory() as session:
            result = await ArticleCompletionRepository(session).persist_completed(
                ready, advanced
            )
            await session.commit()

        if not result.pending_deleted:
            logger.info(
                "article_completion_stale_attempt_ignored",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                attempt_count=ready.attempt_count,
                canonical_url=str(canonical_url),
            )
            return None

        if result.article_id is None:
            logger.info(
                "article_completion_conflict_lost",
                pending_id=ready.pending_id,
                source_id=ready.source_id,
                canonical_url=str(canonical_url),
            )
            return None

        logger.info(
            "article_completion_succeeded",
            pending_id=ready.pending_id,
            source_id=ready.source_id,
            article_id=result.article_id,
            canonical_url=str(canonical_url),
        )
        return result.article_id
