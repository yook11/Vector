"""Briefing stage の監査イベントを組み立てる。"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BasePipelineEventPayload, BriefingPayload
from app.audit.error_chain import extract_error_chain
from app.audit.error_fields import error_message_of, exception_fqn
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.errors import BriefingError
from app.models.category import Category


class BriefingOutcomeCode(StrEnum):
    """Stage.BRIEFING の outcome code。"""

    GENERATION_COMPLETED = "briefing_generation_completed"
    GENERATION_INPUT_EMPTY = "briefing_generation_input_empty"
    GENERATION_ALREADY_EXISTS = "briefing_generation_already_exists"
    DISPATCH_COMPLETED = "briefing_dispatch_completed"
    CATEGORY_ENQUEUED = "briefing_category_enqueued"
    CATEGORY_ENQUEUE_FAILED = "briefing_category_enqueue_failed"
    DISPATCH_CATEGORY_MASTER_LOAD_FAILED = (
        "briefing_dispatch_category_master_load_failed"
    )


class BriefingAuditRepository:
    """Briefing 専用の payload / outcome_code / failure projection を決める。"""

    STAGE: ClassVar[Stage] = Stage.BRIEFING

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- generation: 成功 / skip / rejected ------------------------------

    async def append_generation_completed(
        self,
        *,
        ready: ReadyForBriefing,
        article_count: int,
        ai_model: str,
    ) -> None:
        """1カテゴリの briefing 生成成功を記録する。"""
        category_slug = await self._resolve_category_slug(ready.category_id)
        payload = BriefingPayload(
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=category_slug,
            article_count=article_count,
            ai_model=ai_model,
        )
        await self._append_event(
            event_type=EventType.SUCCEEDED,
            outcome_code=BriefingOutcomeCode.GENERATION_COMPLETED.value,
            payload=payload,
        )

    async def append_generation_input_empty(
        self,
        *,
        ready: ReadyForBriefing,
    ) -> None:
        """対象記事ゼロで LLM を呼ばなかったことを記録する。"""
        category_slug = await self._resolve_category_slug(ready.category_id)
        payload = BriefingPayload(
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=category_slug,
            article_count=0,
        )
        await self._append_event(
            event_type=EventType.REJECTED,
            outcome_code=BriefingOutcomeCode.GENERATION_INPUT_EMPTY.value,
            payload=payload,
        )

    async def append_generation_already_exists(
        self,
        *,
        week_start: date,
        category_id: int,
    ) -> None:
        """既存 briefing があり、生成しなかったことを記録する。"""
        category_slug = await self._resolve_category_slug(category_id)
        payload = BriefingPayload(
            week_start=week_start.isoformat(),
            category_id=category_id,
            category_slug=category_slug,
        )
        await self._append_event(
            event_type=EventType.SKIPPED,
            outcome_code=BriefingOutcomeCode.GENERATION_ALREADY_EXISTS.value,
            payload=payload,
        )

    # --- generation: 失敗経路 (subtask Task 層 try/except) ----------------

    async def append_failure(
        self,
        *,
        ready: ReadyForBriefing,
        exc: BriefingError | SQLAlchemyError,
        retry_exhausted: bool | None,
        ai_model: str,
    ) -> None:
        """1カテゴリの briefing 生成失敗を記録する。"""
        category_slug = await self._resolve_category_slug(ready.category_id)
        projection = self._projection_of(exc)
        await self._append_generation_failed_event(
            ready=ready,
            category_slug=category_slug,
            exc=exc,
            retry_exhausted=retry_exhausted,
            ai_model=ai_model,
            projection=projection,
        )

    async def append_unexpected_failure(
        self,
        *,
        ready: ReadyForBriefing,
        exc: BaseException,
        retry_exhausted: bool | None,
        ai_model: str,
    ) -> None:
        """想定外の briefing 生成失敗を unknown として記録する。"""
        category_slug = await self._resolve_category_slug(ready.category_id)
        await self._append_generation_failed_event(
            ready=ready,
            category_slug=category_slug,
            exc=exc,
            retry_exhausted=retry_exhausted,
            ai_model=ai_model,
            projection=unknown_failure_projection(),
        )

    async def _append_generation_failed_event(
        self,
        *,
        ready: ReadyForBriefing,
        category_slug: str | None,
        exc: BaseException,
        retry_exhausted: bool | None,
        ai_model: str,
        projection: FailureProjection,
    ) -> None:
        payload = BriefingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=category_slug,
            ai_model=ai_model,
            retry_exhausted=retry_exhausted,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._append_event(
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            error_class=exception_fqn(exc),
            retryability=projection.retryability,
        )

    # --- dispatch: run summary / category enqueue ------------------------

    async def append_dispatch_completed(
        self,
        *,
        week_start: date,
        selected_category_count: int,
        enqueued_category_count: int,
        failed_category_count: int,
    ) -> None:
        """dispatcher がカテゴリ enqueue ループを完了したことを記録する。"""
        payload = BriefingPayload(
            week_start=week_start.isoformat(),
            selected_category_count=selected_category_count,
            enqueued_category_count=enqueued_category_count,
            failed_category_count=failed_category_count,
        )
        await self._append_event(
            event_type=EventType.SUCCEEDED,
            outcome_code=BriefingOutcomeCode.DISPATCH_COMPLETED.value,
            payload=payload,
        )

    async def append_category_enqueued(
        self,
        *,
        week_start: date,
        category_id: int,
    ) -> None:
        """1カテゴリ分の generation task enqueue 成功を記録する。"""
        category_slug = await self._resolve_category_slug(category_id)
        payload = BriefingPayload(
            week_start=week_start.isoformat(),
            category_id=category_id,
            category_slug=category_slug,
        )
        await self._append_event(
            event_type=EventType.SUCCEEDED,
            outcome_code=BriefingOutcomeCode.CATEGORY_ENQUEUED.value,
            payload=payload,
        )

    async def append_category_enqueue_failed(
        self,
        *,
        week_start: date,
        category_id: int,
        exc: BaseException,
    ) -> None:
        """1カテゴリ分の generation task enqueue 失敗を記録する。"""
        category_slug = await self._resolve_category_slug(category_id)
        projection = _fixed_projection(
            exc,
            code=BriefingOutcomeCode.CATEGORY_ENQUEUE_FAILED.value,
        )
        payload = BriefingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            week_start=week_start.isoformat(),
            category_id=category_id,
            category_slug=category_slug,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._append_event(
            event_type=EventType.FAILED,
            outcome_code=BriefingOutcomeCode.CATEGORY_ENQUEUE_FAILED.value,
            payload=payload,
            error_class=exception_fqn(exc),
            retryability=projection.retryability,
        )

    async def append_dispatch_category_master_load_failed(
        self,
        *,
        week_start: date,
        exc: BaseException,
    ) -> None:
        """dispatcher がカテゴリマスタを読めなかったことを記録する。"""
        projection = _fixed_projection(
            exc,
            code=BriefingOutcomeCode.DISPATCH_CATEGORY_MASTER_LOAD_FAILED.value,
        )
        payload = BriefingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            week_start=week_start.isoformat(),
            retry_exhausted=True,
            error_message=error_message_of(exc),
            error_chain=extract_error_chain(exc),
        )
        await self._append_event(
            event_type=EventType.FAILED,
            outcome_code=BriefingOutcomeCode.DISPATCH_CATEGORY_MASTER_LOAD_FAILED.value,
            payload=payload,
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

    async def _resolve_category_slug(self, category_id: int) -> str | None:
        """``category_id`` から payload 用の slug を引く。"""
        slug = await self._session.scalar(
            select(Category.slug).where(Category.id == category_id)
        )
        return str(slug) if slug is not None else None

    @staticmethod
    def _projection_of(exc: BriefingError | SQLAlchemyError) -> FailureProjection:
        """Briefing marker / DB 例外を projection する。"""
        return project_failure(exc)


def _fixed_projection(exc: BaseException, *, code: str) -> FailureProjection:
    """retryability / failure_kind は保ちつつ outcome_code だけ固定する。"""
    projection = project_failure(exc, fallback_code=code)
    return FailureProjection(
        failure_kind=projection.failure_kind,
        retryability=projection.retryability,
        failure_action=projection.failure_action,
        code=code,
    )
