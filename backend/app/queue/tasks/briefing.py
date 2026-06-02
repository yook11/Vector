"""週次 briefing 生成タスク (dispatcher + per-category subtask)。

スケジュール:
- ``CRON_WEEKLY_BRIEFING`` (UTC) = JST 月曜 00:05
- dispatcher が直近完了週 × 全カテゴリ分の subtask を kiq する

責務分離:
- ``dispatch_weekly_briefings``: cron 駆動、subtask を kiq するだけの薄い gatekeeper。
  カテゴリ単位の enqueue 監査と週 1 行の summary 監査を焼く。
- ``generate_briefing_for_category``: 1 カテゴリ × 1 週の生成。Service に委譲。
  失敗は監査に焼いた上で raise (taskiq の retry / failure tracking を維持)
- precondition (既存 briefing 判定) は ``ReadyForBriefing.try_advance_from`` 側

エラー方針 (``feedback_failure_visibility.md``):
- subtask は監査に焼いた後 raise し、taskiq の retry を継続する。
- dispatcher は 1 カテゴリの enqueue 失敗を監査して続行する。
- 既存 briefing あり (Ready が None) は skipped audit を焼いて正常終了する。
"""

from __future__ import annotations

from datetime import date

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.audit.stages.briefing import (
    OUTCOME_BRIEFING_CATEGORY_ENQUEUE_FAILED,
    OUTCOME_BRIEFING_CATEGORY_ENQUEUED,
    OUTCOME_BRIEFING_DISPATCH_CATEGORY_MASTER_LOAD_FAILED,
    OUTCOME_BRIEFING_DISPATCH_COMPLETED,
    OUTCOME_BRIEFING_GENERATION_ALREADY_EXISTS,
    BriefingAuditRepository,
)
from app.config import settings
from app.insights.briefing.application.notifier import FrontendRevalidateNotifier
from app.insights.briefing.application.service import WeeklyBriefingService
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.domain.week import latest_completed_week_start, now_in_jst
from app.insights.briefing.llm.errors import BriefingError
from app.insights.briefing.repository.briefings import BriefingRepository
from app.models.category import Category
from app.queue.brokers import broker_briefing
from app.queue.messages.briefing import BriefingTaskInput
from app.queue.retry import is_last_attempt
from app.queue.schedule import CRON_WEEKLY_BRIEFING

logger = structlog.get_logger(__name__)


@broker_briefing.task(
    task_name="dispatch_weekly_briefings",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_WEEKLY_BRIEFING}],
)
async def dispatch_weekly_briefings(ctx: Context = TaskiqDepends()) -> None:
    """JST 月曜 00:05、直近完了週の全カテゴリ briefing を subtask として kiq する。

    カテゴリ取得失敗は全体失敗として re-raise する。カテゴリ単位 enqueue 失敗は
    監査して続行し、最後に count summary を焼く。
    """
    session_factory: async_sessionmaker[AsyncSession] = ctx.state.session_factory
    week_start = latest_completed_week_start(now_in_jst())
    try:
        categories = await _load_briefing_categories(session_factory)
    except Exception as exc:
        await _append_dispatch_category_master_load_failed_audit(
            session_factory,
            week_start=week_start,
            exc=exc,
        )
        raise

    enqueued_count = 0
    failed_count = 0
    for category in categories:
        try:
            await generate_briefing_for_category.kiq(
                BriefingTaskInput(week_start=week_start, category_id=category.id)
            )
        except Exception as exc:
            failed_count += 1
            await _append_category_enqueue_failed_audit(
                session_factory,
                week_start=week_start,
                category_id=category.id,
                exc=exc,
            )
            logger.warning(
                "briefing_category_enqueue_failed",
                week_start=week_start.isoformat(),
                category_id=category.id,
                error_class=_fqn(exc),
                error_message=str(exc) or None,
            )
            continue

        enqueued_count += 1
        await _append_category_enqueued_audit(
            session_factory,
            week_start=week_start,
            category_id=category.id,
        )

    await _append_dispatch_completed_audit(
        session_factory,
        week_start=week_start,
        selected_category_count=len(categories),
        enqueued_category_count=enqueued_count,
        failed_category_count=failed_count,
    )
    logger.info(
        "briefing_dispatch_completed",
        week_start=week_start.isoformat(),
        selected_category_count=len(categories),
        enqueued_category_count=enqueued_count,
        failed_category_count=failed_count,
    )


async def _load_briefing_categories(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[Category]:
    """dispatcher が enqueue 対象にするカテゴリマスタを読む。"""
    async with session_factory() as session:
        rows = await session.execute(select(Category).order_by(Category.id))
        return list(rows.scalars().all())


async def _append_dispatch_completed_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    week_start: date,
    selected_category_count: int,
    enqueued_category_count: int,
    failed_category_count: int,
) -> None:
    try:
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_dispatch_completed(
                week_start=week_start,
                selected_category_count=selected_category_count,
                enqueued_category_count=enqueued_category_count,
                failed_category_count=failed_category_count,
            )
            await session.commit()
    except Exception as exc:
        logger.info(
            "briefing_dispatch_audit_dropped",
            outcome_code=OUTCOME_BRIEFING_DISPATCH_COMPLETED,
            week_start=week_start.isoformat(),
            error_class=_fqn(exc),
            error_message=str(exc) or None,
        )


async def _append_category_enqueued_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    week_start: date,
    category_id: int,
) -> None:
    try:
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_category_enqueued(
                week_start=week_start,
                category_id=category_id,
            )
            await session.commit()
    except Exception as exc:
        logger.info(
            "briefing_dispatch_audit_dropped",
            outcome_code=OUTCOME_BRIEFING_CATEGORY_ENQUEUED,
            week_start=week_start.isoformat(),
            category_id=category_id,
            error_class=_fqn(exc),
            error_message=str(exc) or None,
        )


async def _append_category_enqueue_failed_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    week_start: date,
    category_id: int,
    exc: BaseException,
) -> None:
    try:
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_category_enqueue_failed(
                week_start=week_start,
                category_id=category_id,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.info(
            "briefing_dispatch_audit_dropped",
            outcome_code=OUTCOME_BRIEFING_CATEGORY_ENQUEUE_FAILED,
            week_start=week_start.isoformat(),
            category_id=category_id,
            error_class=_fqn(audit_exc),
            error_message=str(audit_exc) or None,
        )


async def _append_dispatch_category_master_load_failed_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    week_start: date,
    exc: BaseException,
) -> None:
    try:
        async with session_factory() as session:
            await BriefingAuditRepository(
                session
            ).append_dispatch_category_master_load_failed(
                week_start=week_start,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.info(
            "briefing_dispatch_audit_dropped",
            outcome_code=OUTCOME_BRIEFING_DISPATCH_CATEGORY_MASTER_LOAD_FAILED,
            week_start=week_start.isoformat(),
            error_class=_fqn(audit_exc),
            error_message=str(audit_exc) or None,
        )


@broker_briefing.task(
    task_name="generate_briefing_for_category",
    timeout=300,
    max_retries=2,
    retry_on_error=True,
)
async def generate_briefing_for_category(
    input_: BriefingTaskInput,
    ctx: Context = TaskiqDepends(),
) -> None:
    """1 カテゴリ × 1 週の briefing を生成する。失敗は監査して raise する。"""
    session_factory: async_sessionmaker[AsyncSession] = ctx.state.session_factory
    async with session_factory() as session:
        repo = BriefingRepository(session)
        ready = await ReadyForBriefing.try_advance_from(
            week_start=input_.week_start,
            category_id=input_.category_id,
            force=False,
            briefing_repo=repo,
        )
    if ready is None:
        logger.info(
            "briefing_subtask_skipped_existing",
            week_start=input_.week_start.isoformat(),
            category_id=input_.category_id,
        )
        await _append_generation_already_exists_audit(
            session_factory,
            week_start=input_.week_start,
            category_id=input_.category_id,
        )
        return

    notifier = FrontendRevalidateNotifier(
        frontend_base_url=settings.internal_frontend_base_url,
        secret=settings.revalidate_bearer_secret.get_secret_value(),
    )
    # generator は composition root が broker_briefing 起動時に state へ wire する
    # (Pure DI / 遅延 SDK import: app/queue/composition.py)。
    service = WeeklyBriefingService(
        session_factory, ctx.state.briefing_generator, notifier
    )
    # 失敗は監査に焼いた上で raise する (taskiq の retry / failure tracking を維持)。
    # `is_last_attempt(ctx)` で extrinsic な give-up timing を判定し、retry 上限到達時
    # のみ payload.retry_exhausted=True を焼く。
    try:
        outcome = await service.execute(ready)
    except Exception as exc:
        async with session_factory() as session:
            repo = BriefingAuditRepository(session)
            retry_exhausted = True if is_last_attempt(ctx) else None
            if isinstance(exc, (BriefingError, SQLAlchemyError)):
                await repo.append_failure(
                    ready=ready,
                    exc=exc,
                    retry_exhausted=retry_exhausted,
                    ai_model=service._llm.MODEL,
                )
            else:
                await repo.append_unexpected_failure(
                    ready=ready,
                    exc=exc,
                    retry_exhausted=retry_exhausted,
                    ai_model=service._llm.MODEL,
                )
            await session.commit()
        raise
    logger.info(
        "briefing_subtask_completed",
        week_start=outcome.week_start.isoformat(),
        category_id=outcome.category_id,
        article_count=outcome.article_count,
        persisted=outcome.persisted,
    )


async def _append_generation_already_exists_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    week_start: date,
    category_id: int,
) -> None:
    try:
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_generation_already_exists(
                week_start=week_start,
                category_id=category_id,
            )
            await session.commit()
    except Exception as exc:
        logger.info(
            "briefing_generation_audit_dropped",
            outcome_code=OUTCOME_BRIEFING_GENERATION_ALREADY_EXISTS,
            week_start=week_start.isoformat(),
            category_id=category_id,
            error_class=_fqn(exc),
            error_message=str(exc) or None,
        )


def _fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
