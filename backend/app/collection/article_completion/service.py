"""``ArticleCompletionService`` — 未完成記事を完成形に補完する use case。

``pending_html_articles`` 駆動。Task 層が構築した厚い Ready を
``execute(ready)`` で受け取り、成功主線 (HTML 取得 + 抽出 + promotion +
永続化) を担う。重複は ``articles.source_url UNIQUE`` が防ぎ、race-loss は
read-back せず log + ``None`` で短絡する。

- 完成は ``ArticleHtmlCompleter`` (純粋境界) に委譲し
  ``AnalyzableArticle | CompletionFailure`` を値で受け取る。
- ``articles`` INSERT + ``pending_html_articles`` DELETE は同 tx で一括
  commit。真の DB 異常は例外として伝播。
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

from app.collection.article_completion.acquirer import ArticleHtmlAcquirer
from app.collection.article_completion.acquisition_failure import (
    NotHtml,
    ParseCrashed,
    ParserRejected,
    QualityGateFailed,
    classify_acquisition_failure,
    classify_external_fetch_error,
)
from app.collection.article_completion.completer import (
    ArticleHtmlCompleter,
    FetchFailed,
)
from app.collection.article_completion.completion_failure import (
    CompletionInvariantRejected,
    PublishedAtMissing,
    classify_article_completion_failure,
)
from app.collection.article_completion.failure_handling import (
    ArticleCompletionFailureHandler,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.domain.analyzable_article import AnalyzableArticle

logger = structlog.get_logger(__name__)


class ArticleCompletionService:
    """Ready 1 件を HTML 取得 + 永続化する。

    ``execute(ready)`` が単一エントリポイント。完成は ``ArticleHtmlCompleter``
    (純粋境界) に委譲し ``AnalyzableArticle | CompletionFailure`` を値で受け取る。
    失敗は outcome を 3-way ``match`` で concern 別に分類し
    ``ArticleCompletionFailureHandler`` の 2 入口に委譲 (retry は DB 駆動、
    caller に raise しない)。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        acquirer_factory: Callable[[], ArticleHtmlAcquirer] = ArticleHtmlAcquirer,
    ) -> None:
        self._session_factory = session_factory
        self._completer = ArticleHtmlCompleter(acquirer_factory)
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
        outcome = await self._completer.complete(ready)
        match outcome:
            case AnalyzableArticle():
                advanced = outcome
            case FetchFailed(error=err):
                await self._failure_handler.handle_acquisition_failure(
                    ready, classify_external_fetch_error(err), exc=err
                )
                return None
            case NotHtml() | ParserRejected() | ParseCrashed() | QualityGateFailed():
                await self._failure_handler.handle_acquisition_failure(
                    ready, classify_acquisition_failure(outcome), exc=None
                )
                return None
            case PublishedAtMissing() | CompletionInvariantRejected():
                await self._failure_handler.handle_completion_rejected(
                    ready, classify_article_completion_failure(outcome)
                )
                return None
            case _ as unreachable:
                assert_never(unreachable)

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
