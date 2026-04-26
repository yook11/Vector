"""分析タスク — パイプラインの後段。

collection.tasks.fetch_content から呼び出される。
extract_content → classify_content → generate_embedding
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis.classification.service import (
    AlreadyClassifiedOutcome,
    ClassificationService,
    ClassifiedOutcome,
)
from app.analysis.classifier.factory import get_classifier
from app.analysis.embedder.factory import get_embedder
from app.analysis.embedding.service import (
    AlreadyEmbeddedOutcome,
    EmbeddedOutcome,
    EmbeddingService,
)
from app.analysis.embedding.service import (
    SkippedOutcome as EmbeddingSkippedOutcome,
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
from app.analysis.extraction.extractor.factory import get_extractor
from app.analysis.extraction.service import ExtractionService
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_analysis, broker_embedding, is_last_attempt

if TYPE_CHECKING:
    from app.analysis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter construction
# ---------------------------------------------------------------------------


def _build_limiters(
    role: Literal["extract", "classify", "embed"],
    model: str,
    rpm: int | None,
    rpd: int | None,
) -> tuple[RateLimiter | None, RateLimiter | None]:
    """役割 (extract/classify/embed) ごとに独立した RPM/RPD リミッターを構築する。

    role を Redis キーに含めることで、同一モデルを複数役割で使う場合でも
    レート制御カウンターが共有されない。

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
            key=f"ratelimit:{role}:{model}:rpm",
            max_requests=rpm,
            window_seconds=60,
            block=True,
        )
    if rpd is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{role}:{model}:rpd",
            max_requests=rpd,
            window_seconds=86400,
            block=False,
        )
    return rpm_limiter, rpd_limiter


# ---------------------------------------------------------------------------
# Extraction (Stage 1)
# ---------------------------------------------------------------------------


@broker_analysis.task(
    task_name="extract_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def extract_content(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事に対して事実抽出（Stage 1）を実行する。"""
    session_factory = ctx.state.session_factory
    extractor = get_extractor()

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
        logger.warning("extract_content_daily_quota", article_id=article_id)
        return

    # Service 呼び出し（session は内部で管理）
    svc = ExtractionService(session_factory)
    try:
        extraction = await svc.execute(article_id, extractor)
    except (ConfigurationError, DailyQuotaExhaustedError) as e:
        logger.warning(
            "extract_content_no_retry",
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
                "extract_content_max_retries",
                article_id=article_id,
                reason=str(e),
            )
            return
        raise

    # 次ステップへチェーン（extraction が Extraction Entity として返るとき）
    if extraction is not None:
        await classify_content.kiq(article_id)


# ---------------------------------------------------------------------------
# Classification (Stage 2)
# ---------------------------------------------------------------------------


@broker_analysis.task(
    task_name="classify_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def classify_content(
    article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事に対して分類（Stage 2）を実行する。"""
    session_factory = ctx.state.session_factory
    classifier = get_classifier()

    # Rate limit acquire は呼び出し側の責任
    rpm_limiter, rpd_limiter = _build_limiters(
        "classify", classifier.MODEL, classifier.RPM, classifier.RPD
    )
    try:
        if rpd_limiter is not None:
            await rpd_limiter.acquire()
        if rpm_limiter is not None:
            await rpm_limiter.acquire()
    except _RateLimitExceededError:
        logger.warning("classify_content_daily_quota", article_id=article_id)
        return

    # Service 呼び出し（session は内部で管理）
    svc = ClassificationService(session_factory)
    try:
        result = await svc.execute(article_id, classifier)
    except (ConfigurationError, DailyQuotaExhaustedError) as e:
        logger.warning(
            "classify_content_no_retry",
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
                "classify_content_max_retries",
                article_id=article_id,
                reason=str(e),
            )
            return
        raise

    # 次ステップへチェーン (Classified / AlreadyClassified のみ embedding に進む)
    if isinstance(result, (ClassifiedOutcome, AlreadyClassifiedOutcome)):
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
        "embed", embedder.MODEL, embedder.RPM, embedder.RPD
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
        result = await svc.execute(article_id, embedder)
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

    # Outcome に応じた task 層サマリーログ (Service 内のドメインログとは別軸)
    if isinstance(result, EmbeddedOutcome):
        logger.info(
            "generate_embedding_completed",
            article_id=article_id,
            analysis_id=result.embedding.analysis_id,
            model=result.embedding.model_name,
        )
    elif isinstance(result, AlreadyEmbeddedOutcome):
        logger.info(
            "generate_embedding_already_exists",
            article_id=article_id,
            analysis_id=result.embedding.analysis_id,
        )
    elif isinstance(result, EmbeddingSkippedOutcome):
        logger.info(
            "generate_embedding_skipped",
            article_id=article_id,
            reason=result.reason,
        )
