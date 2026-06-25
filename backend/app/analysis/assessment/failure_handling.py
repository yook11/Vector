"""Stage 4 の error handling policy を実行する application service。

Layer 1 marker (``AssessmentTerminalError`` / ``AssessmentRecoverableError`` /
catch-all) を audit / inline retry decision に対応づける唯一の場所。Task 層は
taskiq retry / stage hold の decision だけを解釈する。

Stage 4 は内容起因 DELETE 経路を持たない (curation を保持して assessment を
skip する設計) ため、Stage 3 と Handler は共有しない。
"""

from __future__ import annotations

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderStateError,
)
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentTerminalError,
)
from app.analysis.assessment.metrics import record_assessment_processing_outcome
from app.analysis.failure_handling import FailureHandlingDecision
from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.assessment import AssessmentAuditRepository
from app.shared.security.redaction import redact_secrets

logger = structlog.get_logger(__name__)


def _hold_reason(
    exc: AssessmentRecoverableError | AssessmentTerminalError,
) -> str | None:
    """provider error の回復クラスから stage hold reason を導出する。

    どの回復クラスが hold を要するかは ``AIProviderFailureMode.is_stage_hold_mode``
    が SSoT (marker 型には背負わせない)。hold reason には provider CODE
    (= ``exc.code``) を使い過去 hold metric との連続性を保つ。provider 由来でない
    失敗 (parse の ResponseInvalid 等) は hold しない。
    """
    provider_error = exc.provider_error
    if not isinstance(provider_error, AIProviderStateError | AIProviderContentError):
        return None
    return exc.code if provider_error.FAILURE_MODE.is_stage_hold_mode else None


class AssessmentFailureHandler:
    """Stage 4 の失敗分類に応じた後処理を実行する application service。

    全 marker で best-effort failure audit (DB 落ち時は log fallback) を実行し、
    taskiq に raise すべきか、stage hold を立てるべきかを decision で返す。
    branch ごとの分類ログも本 class 内で完結させ、task 層は marker の意味を
    知らずに済む。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        ready: ReadyForAssessment,
        exc: BaseException,
        last_attempt: bool,
    ) -> FailureHandlingDecision:
        """marker dispatch を実行する。

        失敗分類を確定する境界として ``processing_outcome`` も emit する。分類は match
        時点で確定するため、audit などの副作用より先に emit し、audit drop でも取りこぼ
        さない。SQLAlchemyError は infra_error (成功率の分母外)、それ以外は failed。

        Returns:
            taskiq retry と stage hold の decision。
        """
        match exc:
            case AssessmentTerminalError():
                record_assessment_processing_outcome("failed")
                hold_reason = _hold_reason(exc)
                logger.warning(
                    "assess_content_terminal",
                    curation_id=ready.curation_id,
                    code=exc.code,
                    held=hold_reason is not None,
                )
                await self._audit_failure(ready, exc)
                return FailureHandlingDecision(
                    reraise=False,
                    stage_hold_reason=hold_reason,
                )
            case AssessmentRecoverableError():
                record_assessment_processing_outcome("failed")
                recoverable = exc
                await self._audit_failure(ready, recoverable)
                if last_attempt:
                    hold_reason = _hold_reason(recoverable)
                    logger.warning(
                        "assess_content_recoverable_exhausted",
                        curation_id=ready.curation_id,
                        code=recoverable.code,
                        held=hold_reason is not None,
                    )
                    return FailureHandlingDecision(
                        reraise=False,
                        stage_hold_reason=hold_reason,
                    )
                return FailureHandlingDecision(reraise=True)
            case SQLAlchemyError():
                record_assessment_processing_outcome("infra_error")
                await self._audit_failure(ready, exc)
                return FailureHandlingDecision(reraise=not last_attempt)
            case _:
                record_assessment_processing_outcome("failed")
                await self._audit_unexpected_failure(ready, exc)
                if last_attempt:
                    logger.exception(
                        "assess_content_unexpected_exhausted",
                        curation_id=ready.curation_id,
                    )
                    return FailureHandlingDecision(reraise=False)
                return FailureHandlingDecision(reraise=True)

    async def _audit_failure(
        self,
        ready: ReadyForAssessment,
        exc: AssessmentError | SQLAlchemyError,
    ) -> None:
        """best-effort failure audit (DB 落ち / schema 不整合は log fallback)。

        SDK exception message に key prefix / Authorization header が混入し
        うるため、log 経路にも ``redact_secrets`` を通す (red-team chain γ-2、
        Stage 3 と同 pattern)。
        """
        try:
            async with self._session_factory() as session:
                await AssessmentAuditRepository(session).append_failure(
                    ready=ready, exc=exc
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "assessment_failure_audit_dropped",
                curation_id=ready.curation_id,
                business_error_class=(exception_fqn(exc)),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(exception_fqn(audit_exc)),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
            record_audit_dropped(Stage.ASSESSMENT)

    async def _audit_unexpected_failure(
        self,
        ready: ReadyForAssessment,
        exc: BaseException,
    ) -> None:
        """想定外失敗の best-effort audit。"""
        try:
            async with self._session_factory() as session:
                await AssessmentAuditRepository(session).append_unexpected_failure(
                    ready=ready, exc=exc
                )
                await session.commit()
        except Exception as audit_exc:
            logger.exception(
                "assessment_failure_audit_dropped",
                curation_id=ready.curation_id,
                business_error_class=(exception_fqn(exc)),
                business_error_message=redact_secrets(str(exc))[:500],
                audit_error_class=(exception_fqn(audit_exc)),
                audit_error_message=redact_secrets(str(audit_exc))[:500],
            )
            record_audit_dropped(Stage.ASSESSMENT)
