"""Stage 4 assessment の監査イベントを組み立てる。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildBlockedError,
    ReadyForAssessment,
)
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import AssessmentError
from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import AssessmentPayload, BasePipelineEventPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import error_message_of, exception_fqn
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.ready_build import project_ready_build_failure
from app.audit.repository import PipelineEventRepository
from app.models.backfill_exclusion import BackfillExclusionReason

_INPUT_TEXT_LIMIT = 4096
_AI_RAW_RESPONSE_LIMIT = 2048


class AssessmentOutcomeCode(StrEnum):
    """Stage.ASSESSMENT の outcome code (stage ファイル内定義分のみ)。"""

    IN_SCOPE = "assessed_in_scope"
    OUT_OF_SCOPE = "assessed_out_of_scope"


def _limited_str(value: object, limit: int) -> str | None:
    """非空文字列を上限で切り詰める。"""
    if isinstance(value, str) and value:
        return value[:limit]
    return None


class AssessmentAuditRepository:
    """Stage 4 専用の payload / outcome_code / failure projection を決める。"""

    STAGE: ClassVar[Stage] = Stage.ASSESSMENT
    BACKFILL_STAGE: ClassVar[Stage] = Stage.BACKFILL_ASSESS

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 (in-scope / out-of-scope の業務 INSERT と同 tx) ----------

    async def append_in_scope(
        self,
        *,
        ready: ReadyForAssessment,
        call: AssessmentCall[InScope],
    ) -> None:
        """in-scope 成功を記録する。"""
        in_scope = call.result
        payload = AssessmentPayload(
            curation_id=ready.curation_id,
            ai_model=call.model_name,
            prompt_version=call.prompt_version,
            input_text=ready.summary[:_INPUT_TEXT_LIMIT] or None,
            input_text_length=len(ready.summary),
            ai_raw_response=_limited_str(call.raw_response, _AI_RAW_RESPONSE_LIMIT),
            raw_category=call.raw_category,
            category_slug=in_scope.category.value,
            investor_take=in_scope.investor_take,
        )
        await self._append_event(
            event_type=EventType.SUCCEEDED,
            outcome_code=AssessmentOutcomeCode.IN_SCOPE.value,
            payload=payload,
            article_id=ready.analyzable_article_id,
        )

    async def append_out_of_scope(
        self,
        *,
        ready: ReadyForAssessment,
        call: AssessmentCall[OutOfScope],
    ) -> None:
        """out-of-scope 成功を記録する。"""
        out_of_scope = call.result
        payload = AssessmentPayload(
            curation_id=ready.curation_id,
            ai_model=call.model_name,
            prompt_version=call.prompt_version,
            input_text=ready.summary[:_INPUT_TEXT_LIMIT] or None,
            input_text_length=len(ready.summary),
            ai_raw_response=_limited_str(call.raw_response, _AI_RAW_RESPONSE_LIMIT),
            raw_category=call.raw_category,
            investor_take=out_of_scope.investor_take,
            # category_slug は in-scope 固有のため None
        )
        await self._append_event(
            event_type=EventType.SUCCEEDED,
            outcome_code=AssessmentOutcomeCode.OUT_OF_SCOPE.value,
            payload=payload,
            article_id=ready.analyzable_article_id,
        )

    # --- 救済断念経路 (backfill exclusion と同一 tx) ----------------------

    async def append_backfill_assessment_aged_out(
        self,
        *,
        curation_id: int,
        analyzable_article_id: int,
    ) -> None:
        """古い未 assessment curation を backfill が対象外にした事実を記録する。"""
        await self._append_backfill_event(
            event_type=EventType.REJECTED,
            outcome_code=BackfillExclusionReason.ASSESSMENT_AGED_OUT.value,
            payload=AssessmentPayload(
                curation_id=curation_id,
            ),
            article_id=analyzable_article_id,
        )

    # --- Ready 構築 blocked / failed ---------------------------------------

    async def append_ready_build_blocked(
        self, *, curation_id: int, exc: AssessmentReadyBuildBlockedError
    ) -> None:
        """Ready 構築が domain precondition により進めなかった事実を記録する。

        Domain が reason code で説明できた停止なので rejected として焼く。
        ``article_id`` が判明する経路では top-level に渡して source_id を補填する
        (CURATION_MISSING は対象 curation 不在で article_id なし = source_id 空)。
        """
        await self._append_event(
            event_type=EventType.REJECTED,
            outcome_code=exc.code.value,
            payload=AssessmentPayload(
                curation_id=curation_id,
            ),
            article_id=exc.analyzable_article_id,
        )

    async def append_ready_build_failed(
        self, *, curation_id: int, exc: Exception
    ) -> None:
        """Ready 構築中に blocked 以外の例外が出た事実を failed として記録する。"""
        projection = project_ready_build_failure(stage_prefix=self.STAGE.value, exc=exc)
        payload = AssessmentPayload(
            failure_kind=projection.failure_kind,
            curation_id=curation_id,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._append_event(
            event_type=EventType.FAILED,
            outcome_code=projection.outcome_code,
            payload=payload,
            error_class=exception_fqn(exc),
            retryability=Retryability.UNKNOWN,
        )

    # --- 失敗経路 (Task 層 3 marker dispatch、別 session 別 tx) ----------

    async def append_failure(
        self,
        *,
        ready: ReadyForAssessment,
        exc: AssessmentError | SQLAlchemyError,
    ) -> None:
        """assessment 失敗を記録する。"""
        projection = self._projection_of(exc)
        await self._append_failed_event(ready=ready, exc=exc, projection=projection)

    async def append_unexpected_failure(
        self,
        *,
        ready: ReadyForAssessment,
        exc: BaseException,
    ) -> None:
        """想定外の assessment 失敗を unknown として記録する。"""
        await self._append_failed_event(
            ready=ready,
            exc=exc,
            projection=unknown_failure_projection(),
        )

    async def _append_failed_event(
        self,
        *,
        ready: ReadyForAssessment,
        exc: BaseException,
        projection: FailureProjection,
    ) -> None:
        payload = AssessmentPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            failure_reason=projection.failure_reason,
            curation_id=ready.curation_id,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
            ai_raw_response=_limited_str(
                getattr(exc, "raw_response", None), _AI_RAW_RESPONSE_LIMIT
            ),
        )
        await self._append_event(
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            article_id=ready.analyzable_article_id,
            error_class=exception_fqn(exc),
            retryability=projection.retryability,
        )

    # --- internal helpers -------------------------------------------------

    async def _append_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        await self._events.append(
            stage=self.STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
            retryability=retryability,
        )

    async def _append_backfill_event(
        self,
        *,
        event_type: EventType,
        outcome_code: str,
        payload: BasePipelineEventPayload,
        article_id: int | None = None,
        source_id: int | None = None,
        error_class: str | None = None,
        retryability: Retryability | None = None,
    ) -> None:
        await self._events.append(
            stage=self.BACKFILL_STAGE,
            event_type=event_type,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article_id,
            source_id=source_id,
            error_class=error_class,
            retryability=retryability,
        )

    @staticmethod
    def _projection_of(exc: BaseException) -> FailureProjection:
        """Stage 4 失敗を class attr / adapter から projection する。"""
        return project_failure(exc)
