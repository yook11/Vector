"""Task 層から呼ぶ failure audit 用の application helper。

業務 tx が rollback された後に **別 session で別 tx** として audit を焼くため
session_factory を受ける。内部で ``ExtractionAuditRepository.append_failure``
を呼び commit する。

audit INSERT 自体に失敗した場合は exception を吞んで warning ログを残す
(audit 失敗で業務 task まで死なせない、``_record_failure_event`` と同方針)。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.extraction.audit_repository import ExtractionAuditRepository
from app.analysis.extraction.domain.ready import ReadyForExtraction

logger = structlog.get_logger(__name__)


async def record_extraction_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ready: ReadyForExtraction,
    exc: BaseException,
    attempt: int,
) -> None:
    """Stage 3 failure を pipeline_events に焼付ける (Task 層から呼ぶ)。

    Args:
        session_factory: 別 session を開閉するための factory。
        ready: 失敗対象の Stage 3 入力 (article_id / original_content の出所)。
        exc: 業務 task で raise された exception。``category`` / ``code`` は
            audit_repository が内部で自動導出する。
        attempt: 試行回数 (taskiq retry_count + 1)。
    """
    try:
        async with session_factory() as session:
            await ExtractionAuditRepository(session).append_failure(
                ready=ready,
                exc=exc,
                attempt=attempt,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "extraction_failure_audit_dropped",
            article_id=ready.article_id,
            attempt=attempt,
            business_error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            business_error_message=str(exc)[:500],
            audit_error_class=(
                f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
            ),
            audit_error_message=str(audit_exc)[:500],
        )
