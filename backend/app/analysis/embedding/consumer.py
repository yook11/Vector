"""検証済みイベントからEmbeddingの実行と失敗後処理を進める。"""

from __future__ import annotations

from asyncio import timeout

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.typing import FilteringBoundLogger

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.consumer_failure_classification import (
    NoRetryEmbedding,
    RetryEmbedding,
    classify_embedding_failure,
)
from app.analysis.embedding.consumer_failure_handling import (
    EmbeddingConsumerFailureHandler,
)
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildRejected,
    ReadyForEmbedding,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import (
    EmbeddingCompletion,
    EmbeddingService,
)
from app.audit.error_fields import exception_fqn


class EmbeddingConsumer:
    """1イベントの正常完了か、後処理を終えた失敗を再試行するかを呼び出し元へ伝える。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: BaseEmbedder,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._service = EmbeddingService(session_factory)
        self._failure_handler = EmbeddingConsumerFailureHandler(session_factory)

    async def consume(
        self, event: ArticleAssessedInScope, *, logger: FilteringBoundLogger
    ) -> EmbeddingCompletion | NoRetryEmbedding | RetryEmbedding:
        """業務処理を60秒に制限し、失敗後処理は期限の外で実行する。"""
        analyzable_article_id: int | None = None
        try:
            async with timeout(60):
                async with self._session_factory() as session:
                    facts = await EmbeddingRepository(session).load_ready_build_facts(
                        event.analyzed_article_id
                    )
                    if facts is not None:
                        analyzable_article_id = facts.analyzable_article_id

                build_result = ReadyForEmbedding.from_facts(
                    event.analyzed_article_id, facts
                )
                if isinstance(build_result, EmbeddingReadyBuildRejected):
                    if build_result.reason.is_idempotent_skip:
                        return EmbeddingCompletion.ALREADY_EMBEDDED
                    rejected = build_result
                else:
                    ready, analyzable_article_id = build_result
                    return await self._service.execute(
                        ready,
                        self._embedder,
                        analyzable_article_id=analyzable_article_id,
                        logger=logger,
                    )
        except Exception as exc:
            failure = classify_embedding_failure(exc)
            try:
                await self._failure_handler.handle(
                    failure=failure,
                    exc=exc,
                    analyzed_article_id=event.analyzed_article_id,
                    analyzable_article_id=analyzable_article_id,
                    provider=self._embedder.provider,
                    logger=logger,
                )
            except Exception as secondary:
                logger.warning(
                    "embedding_consumer_failure_processing_failed",
                    operation="failure_handling",
                    analyzed_article_id=event.analyzed_article_id,
                    business_error_class=exception_fqn(exc),
                    exc_info=secondary,
                )
            return failure

        await self._failure_handler.handle_ready_build_rejected(
            analyzed_article_id=event.analyzed_article_id,
            rejected=rejected,
            logger=logger,
        )
        return NoRetryEmbedding(rejected)
