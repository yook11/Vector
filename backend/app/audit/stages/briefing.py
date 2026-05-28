"""Briefing stage の監査イベントを組み立てる。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BriefingPayload
from app.audit.error_chain import extract_error_chain
from app.audit.failure_projection import (
    FailureProjection,
    failure_action_value,
    project_failure,
    unknown_failure_projection,
)
from app.audit.repository import PipelineEventRepository
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.llm.errors import BriefingError
from app.models.category import Category
from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000

OUTCOME_BRIEFING_GENERATION_COMPLETED = "briefing_generation_completed"
OUTCOME_BRIEFING_GENERATION_INPUT_EMPTY = "briefing_generation_input_empty"
OUTCOME_BRIEFING_GENERATION_ALREADY_EXISTS = "briefing_generation_already_exists"
OUTCOME_BRIEFING_DISPATCH_COMPLETED = "briefing_dispatch_completed"
OUTCOME_BRIEFING_CATEGORY_ENQUEUED = "briefing_category_enqueued"
OUTCOME_BRIEFING_CATEGORY_ENQUEUE_FAILED = "briefing_category_enqueue_failed"
OUTCOME_BRIEFING_DISPATCH_CATEGORY_MASTER_LOAD_FAILED = (
    "briefing_dispatch_category_master_load_failed"
)


class BriefingAuditRepository:
    """Briefing 専用の payload / outcome_code / failure projection を決める。"""

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
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.SUCCEEDED,
            outcome_code=OUTCOME_BRIEFING_GENERATION_COMPLETED,
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
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.REJECTED,
            outcome_code=OUTCOME_BRIEFING_GENERATION_INPUT_EMPTY,
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
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.SKIPPED,
            outcome_code=OUTCOME_BRIEFING_GENERATION_ALREADY_EXISTS,
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
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=projection.stage or Stage.BRIEFING,
            event_type=EventType.FAILED,
            outcome_code=projection.code,
            payload=payload,
            error_class=_fqn(exc),
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
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.SUCCEEDED,
            outcome_code=OUTCOME_BRIEFING_DISPATCH_COMPLETED,
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
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.SUCCEEDED,
            outcome_code=OUTCOME_BRIEFING_CATEGORY_ENQUEUED,
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
            code=OUTCOME_BRIEFING_CATEGORY_ENQUEUE_FAILED,
        )
        payload = BriefingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            week_start=week_start.isoformat(),
            category_id=category_id,
            category_slug=category_slug,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.FAILED,
            outcome_code=OUTCOME_BRIEFING_CATEGORY_ENQUEUE_FAILED,
            payload=payload,
            error_class=_fqn(exc),
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
            code=OUTCOME_BRIEFING_DISPATCH_CATEGORY_MASTER_LOAD_FAILED,
        )
        payload = BriefingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            week_start=week_start.isoformat(),
            retry_exhausted=True,
            error_message=redact_secrets(str(exc))[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=extract_error_chain(exc),
        )
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.FAILED,
            outcome_code=OUTCOME_BRIEFING_DISPATCH_CATEGORY_MASTER_LOAD_FAILED,
            payload=payload,
            error_class=_fqn(exc),
            retryability=projection.retryability,
        )

    # --- internal helpers -------------------------------------------------

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
        stage=Stage.BRIEFING,
    )


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
