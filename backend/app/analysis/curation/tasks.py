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

from app.analysis.assessment.domain.ready import AssessmentTrigger
from app.analysis.assessment.tasks import assess_content
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.domain.ready import (
    CurationTrigger,
    ReadyForCuration,
)
from app.analysis.curation.failure_handling import CurationFailureHandler
from app.analysis.curation.repository import CurationRepository
from app.analysis.curation.service import CurationService
from app.brokers import broker_analysis, is_last_attempt

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
    if not await ctx.state.provider_rate_limit_gate.acquire(curator.rate_policy):
        logger.warning("curate_content_daily_quota", article_id=ready.article_id)
        return

    svc = CurationService(session_factory)
    handler = CurationFailureHandler(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1

    try:
        result = await svc.execute(ready, curator)
    except Exception as exc:
        reraise = await handler.handle(
            ready=ready,
            exc=exc,
            curator=curator,
            attempt=attempt,
            last_attempt=is_last_attempt(ctx),
        )
        if reraise:
            raise
        return

    if result is not None:
        await assess_content.kiq(AssessmentTrigger(curation_id=result))


# PR-E.3 で削除予定。削除条件:
#   1. Redis stream pipeline:analysis に task_name="extract_content" の message 0 件
#      (`XINFO STREAM pipeline:analysis FULL` で確認)
#   2. logfire で task_name="extract_content" の invocation 過去 7 日 0 件
#   3. DLQ に task_name="extract_content" message 0 件
@broker_analysis.task(
    task_name="extract_content",
    timeout=180,
    max_retries=1,
    retry_on_error=True,
)
async def extract_content(
    trigger: CurationTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """DEPRECATED alias — use ``curate_content``. Kept for in-flight message drain.

    PR-E.0 (package rename ``extraction`` → ``curation``) の deploy 後、Redis stream
    に残っている古い ``extract_content`` task message を新 worker が受信できる
    ようにするための alias。受信時の処理は ``curate_content`` に丸投げ。
    """
    await curate_content(trigger, ctx)
