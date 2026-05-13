"""Stage 3 (Extraction) taskiq タスク。

collection 経由で起動され、抽出成功時は assess_content (Stage 4) へ chain する。
失敗時の marker dispatch / audit / DELETE / inline retry decision は
``ExtractionFailureHandler`` (``failure_handling.py``) に委譲し、本 task は
trigger 受信、Ready 構築、rate limit、taskiq retry の raise/return semantics
だけに責務を絞る。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis.assessment.domain.ready import AssessmentTrigger
from app.analysis.assessment.tasks import assess_content
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.domain.ready import (
    ExtractionTrigger,
    ReadyForExtraction,
)
from app.analysis.extraction.failure_handling import ExtractionFailureHandler
from app.analysis.extraction.repository import ExtractionRepository
from app.analysis.extraction.service import ExtractionService
from app.brokers import broker_analysis, is_last_attempt

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="extract_content",
    timeout=180,
    max_retries=1,
    retry_on_error=True,
)
async def extract_content(
    trigger: ExtractionTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事に対して事実抽出 (Stage 3) を実行する。

    順序: Ready 構築 → rate limit acquire → Service.execute → 成功 chain。
    precondition 未充足で AI quota を消費しないよう、Ready 構築を rate limit
    より前に置く (Stage 4 / Stage 5 と対称)。

    失敗は ``ExtractionFailureHandler.handle`` に委譲し、戻り値 ``bool`` で
    taskiq retry を起動するかを決める (raise すると taskiq が retry、return
    すれば retry なし)。marker dispatch は Handler 内に閉じる。
    """
    session_factory = ctx.state.session_factory
    extractor: BaseExtractor = ctx.state.extractor

    # 処理開始時に Ready を構築 (precondition + extractor 入力値の全揃え)
    async with session_factory() as session:
        ready = await ReadyForExtraction.try_advance_from(
            article_id=trigger.article_id,
            repo=ExtractionRepository(session),
        )
    if ready is None:
        logger.info(
            "extract_content_skipped",
            article_id=trigger.article_id,
            reason="precondition_not_met",
        )
        return

    # AI を呼ぶ見込みが立ってから rate limit acquire (Stage 4 / Stage 5 と対称)
    if not await ctx.state.provider_rate_limit_gate.acquire(extractor.rate_policy):
        logger.warning("extract_content_daily_quota", article_id=ready.article_id)
        return

    svc = ExtractionService(session_factory)
    handler = ExtractionFailureHandler(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1

    try:
        result = await svc.execute(ready, extractor)
    except Exception as exc:
        reraise = await handler.handle(
            ready=ready,
            exc=exc,
            extractor=extractor,
            attempt=attempt,
            last_attempt=is_last_attempt(ctx),
        )
        if reraise:
            raise
        return

    if result is not None:
        await assess_content.kiq(AssessmentTrigger(extraction_id=result))
