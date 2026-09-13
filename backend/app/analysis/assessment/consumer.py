"""検証済みイベントからAssessmentの実行と失敗後処理を進める。"""

from __future__ import annotations

from asyncio import timeout

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

logger = structlog.get_logger(__name__)


class AssessmentConsumer:
    """1イベントを正常完了させるか、後処理後に失敗を呼び出し元へ伝える。"""

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
        self, event: ArticleCuratedSignal
    ) -> AssessmentCompletion | AssessmentReadyBuildRejected:
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
                    )
        except Exception as exc:
            try:
                failure = classify_assessment_failure(exc)
                await self._failure_handler.handle(
                    failure=failure,
                    exc=exc,
                    curation_id=event.curation_id,
                    analyzable_article_id=analyzable_article_id,
                    provider=self._assessor.provider,
                )
            except Exception as secondary:
                try:
                    logger.warning(
                        "assessment_consumer_failure_processing_failed",
                        curation_id=event.curation_id,
                        business_error_class=exception_fqn(exc),
                        secondary_error_class=exception_fqn(secondary),
                    )
                except Exception:  # noqa: S110
                    # 後処理とログが失敗しても元の処理例外を維持する。
                    pass
            raise

        await self._failure_handler.handle_ready_build_rejected(
            curation_id=event.curation_id, rejected=rejected
        )
        return rejected
