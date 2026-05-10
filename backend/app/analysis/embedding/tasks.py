"""Stage E (Embedding) taskiq タスク。

パイプライン終端 — assess_content から chain される。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis._limiter_factory import _build_limiters
from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.service import (
    EmbeddedOutcome,
    EmbeddingService,
)
from app.analysis.embedding.service import (
    InvalidInputOutcome as EmbeddingInvalidInputOutcome,
)
from app.analysis.errors import (
    ConfigurationError,
    DailyQuotaExhaustedError,
    NetworkError,
    ProviderError,
    UnclassifiedError,
)
from app.analysis.errors import (
    RateLimitError as AnalysisRateLimitError,
)
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_embedding, is_last_attempt

logger = structlog.get_logger(__name__)


@broker_embedding.task(
    task_name="generate_embedding",
    timeout=60,
    max_retries=2,
    retry_on_error=True,
)
async def generate_embedding(
    ready: ReadyForEmbedding,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一 analysis に対してベクトル埋め込みを生成する (Stage E)。

    Pattern A': 受け取った Ready 型は precondition (analysis 存在 + embedding
    未生成 + text 非空) を構造保証している。本 task は再 fetch / None check を
    行わない。
    """
    session_factory = ctx.state.session_factory
    embedder: BaseEmbedder = ctx.state.embedder

    # Rate limit acquire は呼び出し側の責任
    rpm_limiter, rpd_limiter = _build_limiters(
        "embed", embedder.MODEL, embedder.RPM, embedder.RPD
    )
    try:
        if rpd_limiter is not None:
            await rpd_limiter.acquire()
        if rpm_limiter is not None:
            await rpm_limiter.acquire()
    except _RateLimitExceededError:
        logger.warning(
            "generate_embedding_daily_quota",
            analysis_id=ready.analysis_id,
        )
        return

    # Service 呼び出し（session は内部で管理）
    svc = EmbeddingService(session_factory)
    try:
        result = await svc.execute(ready, embedder)
    except (ConfigurationError, DailyQuotaExhaustedError) as e:
        logger.warning(
            "generate_embedding_no_retry",
            analysis_id=ready.analysis_id,
            reason=str(e),
        )
        return
    except (
        AnalysisRateLimitError,
        ProviderError,
        NetworkError,
        UnclassifiedError,
    ) as e:
        if is_last_attempt(ctx):
            logger.warning(
                "generate_embedding_max_retries",
                analysis_id=ready.analysis_id,
                reason=str(e),
            )
            return
        raise

    # Outcome に応じた task 層サマリーログ (Service 内のドメインログとは別軸)
    if isinstance(result, EmbeddedOutcome):
        logger.info(
            "generate_embedding_completed",
            analysis_id=result.embedding.analysis_id,
            model=result.embedding.model_name,
        )
    elif isinstance(result, EmbeddingInvalidInputOutcome):
        logger.info(
            "generate_embedding_invalid_input",
            analysis_id=ready.analysis_id,
        )
