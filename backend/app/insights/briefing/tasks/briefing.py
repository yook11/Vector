"""週次 briefing 生成タスク (dispatcher + per-category subtask)。

スケジュール:
- ``cron="5 15 * * 0"`` (UTC) = JST 月曜 00:05
- dispatcher が直近完了週 × 全カテゴリ分の subtask を kiq する

責務分離:
- ``dispatch_weekly_briefings``: cron 駆動、subtask を kiq するだけの薄い gatekeeper
- ``generate_briefing_for_category``: 1 カテゴリ × 1 週の生成。Service に委譲
- precondition (既存 briefing 判定) は ``ReadyForBriefing.try_advance_from`` 側

エラー方針 (`feedback_failure_visibility.md`):
- subtask は例外を捕まえずに伝播させる (taskiq の retry / failure tracking に委ねる)
- 1 カテゴリの失敗が他カテゴリに伝播しないよう per-category subtask に分割している
- 既存 briefing あり (Ready が None) は正常終了として扱う
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from taskiq import Context, TaskiqDepends

from app.brokers import broker_briefing
from app.config import settings
from app.insights.briefing.application.notifier import FrontendRevalidateNotifier
from app.insights.briefing.application.service import WeeklyBriefingService
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.domain.task_input import BriefingTaskInput
from app.insights.briefing.llm.deepseek import DeepSeekBriefingGenerator
from app.insights.briefing.repository.briefings import BriefingRepository
from app.insights.briefing.domain.week import latest_completed_week_start, now_in_jst
from app.models.category import Category

logger = structlog.get_logger(__name__)


@broker_briefing.task(
    task_name="dispatch_weekly_briefings",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": "5 15 * * 0"}],
)
async def dispatch_weekly_briefings(ctx: Context = TaskiqDepends()) -> None:
    """JST 月曜 00:05、直近完了週の全カテゴリ briefing を subtask として kiq する。"""
    session_factory = ctx.state.session_factory
    week_start = latest_completed_week_start(now_in_jst())
    async with session_factory() as session:
        rows = await session.execute(select(Category).order_by(Category.id))
        categories = rows.scalars().all()
    for category in categories:
        await generate_briefing_for_category.kiq(
            BriefingTaskInput(week_start=week_start, category_id=category.id)
        )
    logger.info(
        "briefing_dispatch_completed",
        week_start=week_start.isoformat(),
        category_count=len(categories),
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
    """1 カテゴリ × 1 週の briefing を生成する。失敗は raise で taskiq に伝える。"""
    session_factory = ctx.state.session_factory
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
        return

    notifier = FrontendRevalidateNotifier(
        frontend_base_url=settings.internal_frontend_base_url,
        secret=settings.internal_api_secret.get_secret_value(),
    )
    service = WeeklyBriefingService(
        session_factory, DeepSeekBriefingGenerator(), notifier
    )
    outcome = await service.execute(ready)
    logger.info(
        "briefing_subtask_completed",
        week_start=outcome.week_start.isoformat(),
        category_id=outcome.category_id,
        article_count=outcome.article_count,
        persisted=outcome.persisted,
    )
