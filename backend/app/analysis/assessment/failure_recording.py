"""Task 層から呼ぶ Stage 4 (assessment) failure audit 用の application helper。

業務 tx が rollback された後でも audit が残るよう **別 session で別 tx** として
``AssessmentAuditRepository.append_failure`` を呼ぶ。Stage 3
``app.analysis.extraction.failure_recording`` の 1:1 同型 (signature と内部実装の
error swallow 戦略を含めて pattern 維持)。

audit INSERT 自体に失敗した場合は exception を吞んで warning ログを残す
(audit 失敗で業務 task まで死なせない、``_record_failure_event`` と同方針)。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.assessment.audit_repository import AssessmentAuditRepository
from app.analysis.assessment.domain.ready import ReadyForAssessment

logger = structlog.get_logger(__name__)


async def record_assessment_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ready: ReadyForAssessment,
    exc: BaseException,
    attempt: int,
) -> None:
    """Stage 4 failure を pipeline_events に焼付ける (Task 層から呼ぶ)。

    Args:
        session_factory: 別 session を開閉するための factory (業務 tx と独立)。
        ready: 失敗対象の Stage 4 入力 (extraction_id 経由で article_id を逆引き)。
        exc: 業務 task で raise された exception。``category`` / ``code`` は
            audit_repository が exc 型と instance attr ``code`` から自動導出する。
        attempt: 試行回数 (taskiq retry_count + 1)。
    """
    try:
        async with session_factory() as session:
            await AssessmentAuditRepository(session).append_failure(
                ready=ready,
                exc=exc,
                attempt=attempt,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "assessment_failure_audit_dropped",
            extraction_id=ready.extraction_id,
            attempt=attempt,
            business_error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            business_error_message=str(exc)[:500],
            audit_error_class=(
                f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
            ),
            audit_error_message=str(audit_exc)[:500],
        )
