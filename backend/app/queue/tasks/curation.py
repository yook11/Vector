"""Stage 3 (Curation) taskiq タスク。

collection 経由で起動され、curation 成功時は assess_content (Stage 4) へ chain する。
失敗時の marker dispatch / audit / DELETE / inline retry decision は
``CurationFailureHandler`` (``failure_handling.py``) に委譲し、本 task は
trigger 受信、Ready 構築、rate limit、taskiq retry の raise/return semantics
だけに責務を絞る。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.failure_handling import CurationFailureHandler
from app.analysis.curation.repository import CurationRepository
from app.analysis.curation.service import CurationService
from app.queue.brokers import broker_analysis
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.curation import CurationTrigger
from app.queue.retry import is_last_attempt
from app.queue.tasks.assessment import assess_content

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="curate_content",
    timeout=180,
    max_retries=1,
    retry_on_error=True,
)
async def curate_content(
    trigger: CurationTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事に対して curation (Stage 3) を実行する。

    順序: Ready 構築 → rate limit acquire → Service.execute → 成功 chain。
    precondition 未充足で AI quota を消費しないよう、Ready 構築を rate limit
    より前に置く (Stage 4 / Stage 5 と対称)。

    失敗は ``CurationFailureHandler.handle`` に委譲し、戻り値 ``bool`` で
    taskiq retry を起動するかを決める (raise すると taskiq が retry、return
    すれば retry なし)。marker dispatch は Handler 内に閉じる。
    """
    session_factory = ctx.state.session_factory
    curator: BaseCurator = ctx.state.curator

    # 処理開始時に Ready を構築 (precondition + curator 入力値の全揃え)
    async with session_factory() as session:
        ready = await ReadyForCuration.try_advance_from(
            article_id=trigger.article_id,
            repo=CurationRepository(session),
        )
    if ready is None:
        logger.info(
            "curate_content_skipped",
            article_id=trigger.article_id,
            reason="precondition_not_met",
        )
        return

    # AI を呼ぶ見込みが立ってから rate limit acquire (Stage 4 / Stage 5 と対称)
    if not await ctx.state.provider_rate_limit_gate.acquire(curator.rate_limit_policy):
        logger.warning("curate_content_daily_quota", article_id=ready.article_id)
        return

    svc = CurationService(session_factory)
    handler = CurationFailureHandler(session_factory)

    try:
        result = await svc.execute(ready, curator)
    except Exception as exc:
        reraise = await handler.handle(
            ready=ready,
            exc=exc,
            curator=curator,
            last_attempt=is_last_attempt(ctx),
        )
        if reraise:
            raise
        return

    if result is not None:
        await assess_content.kiq(AssessmentTrigger(curation_id=result))
