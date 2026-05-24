"""Stage 4 の error handling policy を実行する application service。

Layer 1 marker (``AssessmentTerminalSkipError`` / ``AssessmentRecoverableError`` /
catch-all) を audit / inline retry decision に対応づける唯一の場所。Task 層は
taskiq retry のために reraise decision (``bool``) だけを解釈する。

Stage 4 は内容起因 DELETE 経路を持たない (extraction を保持して assessment を
skip する設計) ため、Stage 3 と Handler は共有しない。Stage 5 も同型の Handler
を別 PR で導入する想定。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.assessment.audit_repository import AssessmentAuditRepository
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)


class AssessmentFailureHandler:
    """Stage 4 の失敗分類に応じた後処理を実行する application service。

    全 marker で best-effort failure audit (DB 落ち時は log fallback) を実行し、
    taskiq に raise すべきかどうかを ``bool`` で返す。branch ごとの分類ログも
    本 class 内で完結させ、task 層は marker の意味を知らずに済む。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        ready: ReadyForAssessment,
        exc: BaseException,
        attempt: int,
        last_attempt: bool,
    ) -> bool:
        """marker dispatch を実行する。

        Returns:
            taskiq に raise すべきなら ``True``、return すべきなら ``False``。
        """
        match exc:
            case AssessmentTerminalSkipError():
                logger.warning(
                    "assess_content_terminal_skip",
                    curation_id=ready.curation_id,
                    code=getattr(exc, "code", None),
                )
                await self._audit_failure(ready, exc, attempt)
                return False
            case AssessmentRecoverableError():
                await self._audit_failure(ready, exc, attempt)
                if last_attempt:
                    logger.warning(
                        "assess_content_recoverable_exhausted",
                        curation_id=ready.curation_id,
                        code=getattr(exc, "code", None),
                    )
                    return False
                return True
            case _:
                await self._audit_failure(ready, exc, attempt)
                if last_attempt:
                    logger.exception(
                        "assess_content_unexpected_exhausted",
                        curation_id=ready.curation_id,
                    )
                    return False
                return True

    async def _audit_failure(
        self,
        ready: ReadyForAssessment,
        exc: BaseException,
        attempt: int,
    ) -> None:
        """best-effort failure audit (DB 落ち / schema 不整合は log fallback)。

        SDK exception message に key prefix / Authorization header が混入し
        うるため、log 経路にも ``redact_secrets`` を通す (red-team chain γ-2、
        Stage 3 と同 pattern)。
        """
        try:
            async with self._session_factory() as session:
                await AssessmentAuditRepository(session).append_failure(
                    ready=ready, exc=exc, attempt=attempt
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "assessment_failure_audit_dropped",
                curation_id=ready.curation_id,
                attempt=attempt,
                business_error_class=(
                    f"{type(exc).__module__}.{type(exc).__qualname__}"
                ),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(
                    f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
                ),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
