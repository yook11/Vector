"""分析タスク — AI による記事分析とベクトル埋め込み生成。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis import (
    ConfigurationError,
    DailyQuotaExhaustedError,
    NetworkError,
    ProviderError,
    UnclassifiedError,
    get_analyzer,
    get_embedder,
)
from app.analysis import (
    RateLimitError as AnalysisRateLimitError,
)
from app.analysis.embedding_service import EmbeddingService
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.analysis.service import ArticleAnalysisService, mark_article_skipped
from app.tasks.brokers import broker_analysis, broker_embedding, is_last_attempt

if TYPE_CHECKING:
    from app.analysis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter construction
# ---------------------------------------------------------------------------


def _build_limiters(
    model: str,
    rpm: int | None,
    rpd: int | None,
) -> tuple[RateLimiter | None, RateLimiter | None]:
    """モデル用の RPM / RPD レートリミッターを構築する。

    Returns:
        (rpm_limiter, rpd_limiter) のタプル。どちらも None になりうる。
    """
    from app.analysis.rate_limiter import RateLimiter
    from app.redis import get_redis

    redis = get_redis()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if rpm is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{model}:rpm",
            max_requests=rpm,
            window_seconds=60,
            block=True,
        )
    if rpd is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{model}:rpd",
            max_requests=rpd,
            window_seconds=86400,
            block=False,
        )
    return rpm_limiter, rpd_limiter


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@broker_analysis.task(
    task_name="analyze_article",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def analyze_article(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事に対して AI 分析を実行する。"""
    session_factory = ctx.state.session_factory
    analyzer = get_analyzer()

    # Rate limit acquire は呼び出し側の責任
    rpm_limiter, rpd_limiter = _build_limiters(
        analyzer.MODEL, analyzer.RPM, analyzer.RPD
    )
    try:
        if rpd_limiter is not None:
            await rpd_limiter.acquire()
        if rpm_limiter is not None:
            await rpm_limiter.acquire()
    except _RateLimitExceededError:
        logger.warning("analyze_article_daily_quota", article_id=article_id)
        return

    # Service 呼び出し（session は内部で管理）
    svc = ArticleAnalysisService(session_factory)
    try:
        result = await svc.execute(article_id, analyzer)
    except (ConfigurationError, DailyQuotaExhaustedError) as e:
        logger.warning(
            "analyze_article_no_retry",
            article_id=article_id,
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
            if isinstance(e, AnalysisRateLimitError):
                await mark_article_skipped(session_factory, article_id)
            logger.warning(
                "analyze_article_max_retries",
                article_id=article_id,
                reason=str(e),
            )
            return
        raise

    # 次ステップへチェーン
    if result.status in ("created", "already_exists"):
        await generate_embedding.kiq(article_id)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


@broker_embedding.task(
    task_name="generate_embedding",
    timeout=60,
    max_retries=2,
    retry_on_error=True,
)
async def generate_embedding(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事の分析結果に対してベクトル埋め込みを生成する。"""
    session_factory = ctx.state.session_factory
    embedder = get_embedder()

    # Rate limit acquire は呼び出し側の責任
    rpm_limiter, rpd_limiter = _build_limiters(
        embedder.MODEL, embedder.RPM, embedder.RPD
    )
    try:
        if rpd_limiter is not None:
            await rpd_limiter.acquire()
        if rpm_limiter is not None:
            await rpm_limiter.acquire()
    except _RateLimitExceededError:
        logger.warning("generate_embedding_daily_quota", article_id=article_id)
        return

    # Service 呼び出し（session は内部で管理）
    svc = EmbeddingService(session_factory)
    try:
        await svc.execute(article_id, embedder)
    except (ConfigurationError, DailyQuotaExhaustedError) as e:
        logger.warning(
            "generate_embedding_no_retry",
            article_id=article_id,
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
                article_id=article_id,
                reason=str(e),
            )
            return
        raise
