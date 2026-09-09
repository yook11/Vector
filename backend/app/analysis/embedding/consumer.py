"""検証済みイベントからEmbeddingの実行と失敗後処理を進める。"""

from __future__ import annotations

from asyncio import timeout

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.consumer_failure_classification import (
    classify_embedding_failure,
)
from app.analysis.embedding.consumer_failure_handling import (
    EmbeddingConsumerFailureHandler,
)
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildBlockedCode,
    EmbeddingReadyBuildBlockedError,
    ReadyForEmbedding,
)
from app.analysis.embedding.errors import EmbeddingAnalyzedArticleMissingError
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import (
    EmbeddingCompletion,
    EmbeddingCompletionReason,
    EmbeddingService,
)
from app.audit.error_fields import exception_fqn

logger = structlog.get_logger(__name__)


class EmbeddingConsumer:
    """1イベントを正常完了させるか、後処理後に失敗を呼び出し元へ伝える。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: BaseEmbedder,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._service = EmbeddingService(session_factory)
        self._failure_handler = EmbeddingConsumerFailureHandler(session_factory)

    async def consume(self, event: ArticleAssessedInScope) -> EmbeddingCompletion:
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

                try:
                    ready, analyzable_article_id = ReadyForEmbedding.from_facts(
                        event.analyzed_article_id, facts
                    )
                except EmbeddingReadyBuildBlockedError as blocked:
                    if blocked.code is EmbeddingReadyBuildBlockedCode.ALREADY_EMBEDDED:
                        return EmbeddingCompletion(
                            EmbeddingCompletionReason.ALREADY_EMBEDDED
                        )
                    if (
                        blocked.code
                        is EmbeddingReadyBuildBlockedCode.ANALYZED_ARTICLE_MISSING
                    ):
                        raise EmbeddingAnalyzedArticleMissingError() from blocked
                    raise

                return await self._service.execute(
                    ready,
                    self._embedder,
                    analyzable_article_id=analyzable_article_id,
                )
        except Exception as exc:
            try:
                failure = classify_embedding_failure(exc)
                await self._failure_handler.handle(
                    failure=failure,
                    exc=exc,
                    analyzed_article_id=event.analyzed_article_id,
                    analyzable_article_id=analyzable_article_id,
                    provider=self._embedder.provider,
                )
            except Exception as secondary:
                try:
                    logger.warning(
                        "embedding_consumer_failure_processing_failed",
                        analyzed_article_id=event.analyzed_article_id,
                        business_error_class=exception_fqn(exc),
                        secondary_error_class=exception_fqn(secondary),
                    )
                except Exception:  # noqa: S110
                    # 後処理とログが失敗しても元の処理例外を維持する。
                    pass
            raise
