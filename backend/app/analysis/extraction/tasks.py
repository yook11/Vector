"""Stage 3 (Extraction) taskiq タスク。

collection.tasks.fetch_content から chain され、抽出成功時は
assess_content (Stage 4) へ chain する。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis._limiter_factory import _build_limiters
from app.analysis.assessment.domain.ready import AssessmentTrigger
from app.analysis.assessment.tasks import assess_content
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.failure_recording import record_extraction_failure
from app.analysis.extraction.service import (
    ExtractedOutcome,
    ExtractionService,
    NoiseOutcome,
)
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_analysis, is_last_attempt
from app.observability.categories import (
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="extract_content",
    timeout=180,
    max_retries=1,
    retry_on_error=True,
)
async def extract_content(
    ready: ReadyForExtraction,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事に対して事実抽出 (Stage 3) を実行する。

    Pattern A': 受け取った Ready 型は precondition (article 存在 + extraction 未生成
    + 本文サイズ ≤ hard cap) を構造保証している。本 task は再 fetch / None check を
    行わない。

    Layer 1 marker dispatch (spec §Task 層実装):
    - ``NonRetryableDropArticle``: ``svc.mark_article_unprocessable`` で
      audit + DELETE (内容起因 Permanent、記事削除)
    - ``NonRetryableKeepArticle``: audit のみ (記事保持、運用者対応で復旧)
    - ``RetryableError``: ``INLINE_RETRY=True`` かつ ``not is_last_attempt`` なら
      raise (taskiq retry)、それ以外は audit + return (cron 救済委譲)
    - catch-all: audit + return (UNKNOWN ラベル、cron TTL 削除に委譲)
    """
    session_factory = ctx.state.session_factory
    extractor: BaseExtractor = ctx.state.extractor

    # Rate limit acquire は呼び出し側の責任
    rpm_limiter, rpd_limiter = _build_limiters(
        "extract", extractor.MODEL, extractor.RPM, extractor.RPD
    )
    try:
        if rpd_limiter is not None:
            await rpd_limiter.acquire()
        if rpm_limiter is not None:
            await rpm_limiter.acquire()
    except _RateLimitExceededError:
        logger.warning("extract_content_daily_quota", article_id=ready.article_id)
        return

    # Service 呼び出し (session は内部で管理)
    svc = ExtractionService(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1
    try:
        result = await svc.execute(ready, extractor)
    except NonRetryableDropArticle as exc:
        await svc.mark_article_unprocessable(
            ready.article_id,
            ready.original_content,
            code=getattr(type(exc), "CODE", "ai_error_unknown_drop"),
            exc=exc,
        )
        return
    except NonRetryableKeepArticle as exc:
        await record_extraction_failure(
            session_factory,
            ready=ready,
            exc=exc,
            attempt=attempt,
        )
        return
    except RetryableError as exc:
        if type(exc).INLINE_RETRY and not is_last_attempt(ctx):
            raise  # taskiq 即時 retry
        await record_extraction_failure(
            session_factory,
            ready=ready,
            exc=exc,
            attempt=attempt,
        )
        return
    except Exception as exc:
        await record_extraction_failure(
            session_factory,
            ready=ready,
            exc=exc,
            attempt=attempt,
        )
        return

    # Stage 4 を ID で起動 (案 3: 下流 Stage 自身が処理開始時に Ready を構築)。
    if isinstance(result, ExtractedOutcome):
        await assess_content.kiq(
            AssessmentTrigger(extraction_id=result.extraction.id),
        )
    elif isinstance(result, NoiseOutcome):
        logger.info(
            "extract_content_noise",
            article_id=ready.article_id,
        )
