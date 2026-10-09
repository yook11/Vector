"""検証済みイベントからCurationの実行と失敗後処理を進める。"""

from __future__ import annotations

from asyncio import timeout

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.typing import FilteringBoundLogger

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.consumer_failure_classification import (
    NoRetryCuration,
    RetryCuration,
    classify_curation_failure,
)
from app.analysis.curation.consumer_failure_handling import (
    CurationConsumerFailureHandler,
)
from app.analysis.curation.domain.ready import (
    CurationReadyBuildRejected,
    ReadyForCuration,
)
from app.analysis.curation.repository import CurationRepository
from app.analysis.curation.service import (
    CurationCompletion,
    CurationCompletionKind,
    CurationService,
)
from app.audit.error_fields import exception_fqn
from app.collection.events import AnalyzableArticleCreated


class CurationConsumer:
    """1イベントの正常完了か、後処理を終えた失敗を再試行するかを呼び出し元へ伝える。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        curator: BaseCurator,
    ) -> None:
        self._session_factory = session_factory
        self._curator = curator
        self._service = CurationService(session_factory)
        self._failure_handler = CurationConsumerFailureHandler(session_factory)

    async def consume(
        self, event: AnalyzableArticleCreated, *, logger: FilteringBoundLogger
    ) -> CurationCompletion | NoRetryCuration | RetryCuration:
        """業務処理を60秒に制限し、失敗後処理は期限の外で実行する。"""
        analyzable_article_id: int | None = None
        try:
            async with timeout(60):
                async with self._session_factory() as session:
                    facts = await CurationRepository(session).load_ready_build_facts(
                        event.analyzable_article_id
                    )
                    if facts is not None:
                        analyzable_article_id = facts.analyzable_article_id

                build_result = ReadyForCuration.from_facts(facts)
                if isinstance(build_result, CurationReadyBuildRejected):
                    if build_result.reason.is_idempotent_skip:
                        return CurationCompletion(
                            CurationCompletionKind.ALREADY_CURATED
                        )
                    rejected = build_result
                else:
                    return await self._service.execute(
                        build_result, self._curator, logger=logger
                    )
        except Exception as exc:
            failure = classify_curation_failure(exc)
            try:
                await self._failure_handler.handle(
                    failure=failure,
                    exc=exc,
                    target_article_id=event.analyzable_article_id,
                    analyzable_article_id=analyzable_article_id,
                    provider=self._curator.provider,
                    logger=logger,
                )
            except Exception as secondary:
                logger.warning(
                    "curation_consumer_failure_processing_failed",
                    operation="failure_handling",
                    business_error_class=exception_fqn(exc),
                    exc_info=secondary,
                )
            return failure

        await self._failure_handler.handle_ready_build_rejected(
            target_article_id=event.analyzable_article_id,
            rejected=rejected,
            logger=logger,
        )
        return NoRetryCuration(rejected)
