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

OUTCOME_BRIEFING_COMPLETED = "briefing_completed"
OUTCOME_BRIEFING_INPUT_EMPTY = "briefing_input_empty"
OUTCOME_BRIEFING_DISPATCHED = "briefing_dispatched"


class BriefingAuditRepository:
    """Briefing 専用の payload / outcome_code / failure projection を決める。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)

    # --- 成功経路 (subtask) -----------------------------------------------

    async def append_completed(
        self,
        *,
        ready: ReadyForBriefing,
        article_count: int,
        ai_model: str,
    ) -> None:
        """subtask の成功を記録する。"""
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
            outcome_code=OUTCOME_BRIEFING_COMPLETED,
            payload=payload,
        )

    # --- REJECTED 経路 (subtask 入力ゼロ) -------------------------------

    async def append_input_empty(
        self,
        *,
        ready: ReadyForBriefing,
    ) -> None:
        """subtask の入力ゼロを rejected として記録する。"""
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
            outcome_code=OUTCOME_BRIEFING_INPUT_EMPTY,
            payload=payload,
        )

    # --- 失敗経路 (subtask Task 層 try/except) ----------------------------

    async def append_failure(
        self,
        *,
        ready: ReadyForBriefing,
        exc: BriefingError | SQLAlchemyError,
        retry_exhausted: bool | None,
        ai_model: str,
    ) -> None:
        """subtask の失敗を記録する。"""
        category_slug = await self._resolve_category_slug(ready.category_id)
        projection = self._projection_of(exc)
        await self._append_subtask_failed_event(
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
        """想定外の briefing subtask 失敗を unknown として記録する。"""
        category_slug = await self._resolve_category_slug(ready.category_id)
        await self._append_subtask_failed_event(
            ready=ready,
            category_slug=category_slug,
            exc=exc,
            retry_exhausted=retry_exhausted,
            ai_model=ai_model,
            projection=unknown_failure_projection(),
        )

    async def _append_subtask_failed_event(
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

    # --- 成功経路 (dispatcher anchor) -------------------------------------

    async def append_dispatched(
        self,
        *,
        week_start: date,
        category_count: int,
    ) -> None:
        """dispatcher の週次成功 anchor を記録する。"""
        payload = BriefingPayload(
            week_start=week_start.isoformat(),
            category_count=category_count,
        )
        await self._events.append(
            stage=Stage.BRIEFING,
            event_type=EventType.SUCCEEDED,
            outcome_code=OUTCOME_BRIEFING_DISPATCHED,
            payload=payload,
        )

    # --- 失敗経路 (dispatcher 自体の障害) ---------------------------------

    async def append_dispatcher_failure(
        self,
        *,
        week_start: date | None,
        exc: BriefingError | SQLAlchemyError,
    ) -> None:
        """dispatcher 自体の失敗 anchor を記録する。"""
        projection = self._projection_of(exc)
        await self._append_dispatcher_failed_event(
            week_start=week_start,
            exc=exc,
            projection=projection,
        )

    async def append_unexpected_dispatcher_failure(
        self,
        *,
        week_start: date | None,
        exc: BaseException,
    ) -> None:
        """想定外の dispatcher 失敗 anchor を unknown として記録する。"""
        await self._append_dispatcher_failed_event(
            week_start=week_start,
            exc=exc,
            projection=unknown_failure_projection(),
        )

    async def _append_dispatcher_failed_event(
        self,
        *,
        week_start: date | None,
        exc: BaseException,
        projection: FailureProjection,
    ) -> None:
        payload = BriefingPayload(
            failure_kind=projection.failure_kind,
            failure_action=failure_action_value(projection),
            week_start=week_start.isoformat() if week_start is not None else None,
            retry_exhausted=True,
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


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
