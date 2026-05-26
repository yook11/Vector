"""週次 briefing 生成タスク (dispatcher + per-category subtask)。

スケジュール:
- ``CRON_WEEKLY_BRIEFING`` (UTC) = JST 月曜 00:05
- dispatcher が直近完了週 × 全カテゴリ分の subtask を kiq する

責務分離:
- ``dispatch_weekly_briefings``: cron 駆動、subtask を kiq するだけの薄い gatekeeper。
  自身も週 1 行の anchor 監査を焼く (subtask が一切 kiq されない週に痕跡ゼロに
  ならないため)
- ``generate_briefing_for_category``: 1 カテゴリ × 1 週の生成。Service に委譲。
  失敗は監査に焼いた上で raise (taskiq の retry / failure tracking を維持)
- precondition (既存 briefing 判定) は ``ReadyForBriefing.try_advance_from`` 側

エラー方針 (``feedback_failure_visibility.md``):
- subtask は監査に焼いた後 raise し、taskiq の retry を継続する。
- 1 カテゴリの失敗が他カテゴリに伝播しないよう per-category subtask に分割している
- 既存 briefing あり (Ready が None) は正常終了として扱う
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from taskiq import Context, TaskiqDepends

from app.config import settings
from app.insights.briefing.application.notifier import FrontendRevalidateNotifier
from app.insights.briefing.application.service import WeeklyBriefingService
from app.insights.briefing.audit_repository import BriefingAuditRepository
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.domain.week import latest_completed_week_start, now_in_jst
from app.insights.briefing.llm.deepseek import DeepSeekBriefingGenerator
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

    成功 anchor を 1 行焼く (subtask の SUCCEEDED 集計だけでは「dispatcher 自体が
    落ちた週」を SQL から気付けないため)。dispatcher 失敗時は ``max_retries=0``
    で初回即 give-up = ``retry_exhausted=True`` で anchor を焼く。
    """
    session_factory = ctx.state.session_factory
    week_start = latest_completed_week_start(now_in_jst())
    try:
        async with session_factory() as session:
            rows = await session.execute(select(Category).order_by(Category.id))
            categories = rows.scalars().all()
        for category in categories:
            await generate_briefing_for_category.kiq(
                BriefingTaskInput(week_start=week_start, category_id=category.id)
            )
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_dispatched(
                week_start=week_start,
                category_count=len(categories),
            )
            await session.commit()
        logger.info(
            "briefing_dispatch_completed",
            week_start=week_start.isoformat(),
            category_count=len(categories),
        )
    except Exception as exc:
        # 別 session で焼いて re-raise (taskiq の failure tracking を維持)。
        # dispatch 中の例外でも常に week_start は確定済 (cron 起動直後に算出)。
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_dispatcher_failure(
                week_start=week_start,
                exc=exc,
            )
            await session.commit()
        raise


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
        secret=settings.revalidate_bearer_secret.get_secret_value(),
    )
    service = WeeklyBriefingService(
        session_factory, DeepSeekBriefingGenerator(), notifier
    )
    # 失敗は監査に焼いた上で raise する (taskiq の retry / failure tracking を維持)。
    # `is_last_attempt(ctx)` で extrinsic な give-up timing を判定し、最終 attempt
    # のみ payload.retry_exhausted=True を焼く (CompletionPayload precedent)。
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1
    try:
        outcome = await service.execute(ready)
    except Exception as exc:
        async with session_factory() as session:
            await BriefingAuditRepository(session).append_failure(
                ready=ready,
                exc=exc,
                attempt=attempt,
                retry_exhausted=True if is_last_attempt(ctx) else None,
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
