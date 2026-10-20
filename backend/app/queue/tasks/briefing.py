"""週次 briefing 生成タスク (dispatcher + per-category subtask)。

スケジュール:
- ``CRON_WEEKLY_BRIEFING`` (UTC) = JST 月曜 00:05
- dispatcher が直近完了週 × 全カテゴリ分の subtask を kiq する

責務分離:
- ``dispatch_weekly_briefings``: cron 駆動、subtask を kiq するだけの薄い gatekeeper。
- ``generate_briefing_for_category``: 1 カテゴリ × 1 週の生成。Service に委譲。
  失敗は raise して taskiq の retry に委ねる。
- 開始条件は ``ReadyForBriefing.from_facts`` が判定する

エラー方針 (``feedback_failure_visibility.md``):
- dispatcher は 1 カテゴリの enqueue 失敗を log に残して続行する。
- 既存 briefing あり・記事 0 件は log で観測して正常終了する。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.audit.domain.event import Stage
from app.audit.error_fields import exception_fqn
from app.config import settings
from app.insights.briefing.domain.briefing import (
    latest_completed_week_start,
    now_in_jst,
)
from app.insights.briefing.domain.ready import (
    BriefingReadyBuildRejectionReason,
    ReadyForBriefing,
)
from app.insights.briefing.repository import BriefingRepository
from app.insights.briefing.service import BriefingConflict, WeeklyBriefingService
from app.logfire.stage_span import pipeline_stage_span
from app.models.category import Category
from app.queue.brokers import broker_briefing
from app.queue.messages.briefing import BriefingTaskInput
from app.queue.schedule import CRON_WEEKLY_BRIEFING
from app.shared.revalidate import FrontendRevalidateNotifier

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

    カテゴリ取得失敗は全体失敗として raise する。カテゴリ単位 enqueue 失敗は
    log に残して続行し、最後に件数を log に出す。
    """
    session_factory: async_sessionmaker[AsyncSession] = ctx.state.session_factory
    week_start = latest_completed_week_start(now_in_jst())
    categories = await _load_briefing_categories(session_factory)

    enqueued_count = 0
    failed_count = 0
    for category in categories:
        try:
            await generate_briefing_for_category.kiq(
                BriefingTaskInput(week_start=week_start, category_id=category.id)
            )
        except Exception as exc:
            failed_count += 1
            logger.warning(
                "briefing_category_enqueue_failed",
                week_start=week_start.isoformat(),
                category_id=category.id,
                error_class=exception_fqn(exc),
                error_message=str(exc) or None,
            )
            continue

        enqueued_count += 1

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
    """1 カテゴリ × 1 週の briefing を生成する。失敗は raise する。"""
    with pipeline_stage_span(Stage.BRIEFING, op="generate_briefing_for_category"):
        session_factory: async_sessionmaker[AsyncSession] = ctx.state.session_factory
        notifier = FrontendRevalidateNotifier.from_settings(settings)
        # generator は composition root が broker_briefing 起動時に state へ wire する
        # (Pure DI / 遅延 SDK import: app/queue/composition.py)。
        service = WeeklyBriefingService(
            session_factory, ctx.state.briefing_generator, notifier
        )
        async with session_factory() as session:
            facts = await BriefingRepository(session).load_ready_build_facts(
                week_start=input_.week_start, category_id=input_.category_id
            )
        build_result = ReadyForBriefing.from_facts(
            week_start=input_.week_start,
            category_id=input_.category_id,
            facts=facts,
        )
        if build_result is BriefingReadyBuildRejectionReason.ALREADY_GENERATED:
            # 既存 briefing あり = benign な冪等 skip。log で観測する。
            logger.info(
                "briefing_subtask_skipped_existing",
                week_start=input_.week_start.isoformat(),
                category_id=input_.category_id,
            )
            return
        if build_result is BriefingReadyBuildRejectionReason.NO_ARTICLES:
            logger.info(
                "briefing_skip_no_articles",
                week_start=input_.week_start.isoformat(),
                category_id=input_.category_id,
            )
            return
        if build_result is BriefingReadyBuildRejectionReason.CATEGORY_MISSING:
            # dispatch 後に削除された場合だけ起きるため、失敗として表に出す。
            raise ValueError(f"Category not found: id={input_.category_id}")
        outcome = await service.execute(build_result)
        if isinstance(outcome, BriefingConflict):
            # race 敗北は業務正常 (勝者が revalidate を担う)。
            logger.info(
                "briefing_subtask_conflict",
                week_start=outcome.week_start.isoformat(),
                category_id=outcome.category_id,
                article_count=outcome.article_count,
            )
            return
        logger.info(
            "briefing_subtask_completed",
            week_start=outcome.week_start.isoformat(),
            category_id=outcome.category_id,
            article_count=outcome.article_count,
        )
