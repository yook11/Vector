"""検証済みイベントからAssessmentの実行と失敗後処理を進める。"""

from __future__ import annotations

from asyncio import timeout

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.typing import FilteringBoundLogger

from app.ai_providers.errors import AIProviderError
from app.analysis.ai_provider_settlement import (
    SettledProviderFailure,
    settled_provider_failure,
)
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.consumer_failure_classification import (
    classify_assessment_failure,
)
from app.analysis.assessment.consumer_failure_handling import (
    AssessmentConsumerFailureHandler,
)
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildRejected,
    ReadyForAssessment,
)
from app.analysis.assessment.repository import AssessmentRepository
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
    AssessmentService,
)
from app.analysis.curation.events import ArticleCuratedSignal
from app.audit.error_fields import exception_fqn
from app.audit.failure_projection import FailureProjection


class AssessmentConsumer:
    """1イベントを正常完了させるか、後処理後に失敗を受信完了にするか呼び出し元へ伝える。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        assessor: BaseAssessor,
    ) -> None:
        self._session_factory = session_factory
        self._assessor = assessor
        self._service = AssessmentService(session_factory)
        self._failure_handler = AssessmentConsumerFailureHandler(session_factory)

    async def consume(
        self, event: ArticleCuratedSignal, *, logger: FilteringBoundLogger
    ) -> AssessmentCompletion | AssessmentReadyBuildRejected | SettledProviderFailure:
        """業務処理を60秒に制限し、失敗後処理は期限の外で実行する。"""
        analyzable_article_id: int | None = None
        try:
            async with timeout(60):
                async with self._session_factory() as session:
                    facts = await AssessmentRepository(session).load_ready_build_facts(
                        event.curation_id
                    )
                    if facts is not None:
                        analyzable_article_id = facts.analyzable_article_id

                build_result = ReadyForAssessment.from_facts(event.curation_id, facts)
                if isinstance(build_result, AssessmentReadyBuildRejected):
                    if build_result.reason.is_idempotent_skip:
                        return AssessmentCompletion(
                            AssessmentCompletionKind.ALREADY_ASSESSED
                        )
                    rejected = build_result
                else:
                    ready, analyzable_article_id = build_result
                    return await self._service.execute(
                        ready,
                        self._assessor,
                        analyzable_article_id=analyzable_article_id,
                        logger=logger,
                    )
        except Exception as exc:
            projection: FailureProjection | None = None
            try:
                projection = classify_assessment_failure(exc)
                await self._failure_handler.handle(
                    projection=projection,
                    exc=exc,
                    curation_id=event.curation_id,
                    analyzable_article_id=analyzable_article_id,
                    provider=self._assessor.provider,
                    logger=logger,
                )
            except Exception as secondary:
                logger.warning(
                    "assessment_consumer_failure_processing_failed",
                    operation="failure_handling",
                    curation_id=event.curation_id,
                    business_error_class=exception_fqn(exc),
                    exc_info=secondary,
                )
            # 分類できた失敗だけを受信完了の対象にし、後処理の失敗では再配信に戻さない。
            if projection is not None and isinstance(exc, AIProviderError):
                settled = settled_provider_failure(exc)
                if settled is not None:
                    return settled
            raise

        await self._failure_handler.handle_ready_build_rejected(
            curation_id=event.curation_id, rejected=rejected, logger=logger
        )
        return rejected
