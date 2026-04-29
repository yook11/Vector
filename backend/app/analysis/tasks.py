"""分析タスク — パイプラインの後段。

collection.tasks.fetch_content から呼び出される。
extract_content → classify_content → generate_embedding
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis.classification.domain.ready import ReadyForClassification
from app.analysis.classification.rejection_repository import RejectionRepository
from app.analysis.classification.repository import AnalysisRepository
from app.analysis.classification.service import (
    ClassificationService,
    ClassifiedOutcome,
)
from app.analysis.classifier.base import BaseClassifier
from app.analysis.embedder.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import (
    EmbeddedOutcome,
    EmbeddingService,
    InvalidInputOutcome,
)
from app.analysis.errors import (
    ConfigurationError,
    DailyQuotaExhaustedError,
    InsufficientBalanceError,
    NetworkError,
    ProviderError,
    UnclassifiedError,
)
from app.analysis.errors import (
    RateLimitError as AnalysisRateLimitError,
)
from app.analysis.extraction.extractor.base import BaseExtractor
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

    # Stage D へ chain (Pattern A': 上流 Task が下流 Ready を構築 — spec §7.1)
    if extraction is not None:
        async with session_factory() as session:
            analysis_repo = AnalysisRepository(session)
            rejection_repo = RejectionRepository(session)
            ready = await ReadyForClassification.try_advance_from(
                extraction,
                analysis_repo=analysis_repo,
                rejection_repo=rejection_repo,
            )
        if ready is not None:
            await classify_content.kiq(ready)


# ---------------------------------------------------------------------------
# Classification (Stage D)
# ---------------------------------------------------------------------------


@broker_analysis.task(
    task_name="classify_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def classify_content(
    ready: ReadyForClassification,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一 extraction に対して分類 (Stage D) を実行する。

    Pattern A': 受け取った Ready 型は precondition (extraction 存在 + 未分類 +
    未却下) を構造保証している。本 task は再 fetch / None check を行わない。
    """
    session_factory = ctx.state.session_factory
    classifier: BaseClassifier = ctx.state.classifier

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
        logger.warning(
            "classify_content_daily_quota",
            extraction_id=ready.extraction_id,
        )
        return

    # Service 呼び出し（session は内部で管理）
    svc = ClassificationService(session_factory)
    try:
        result = await svc.execute(ready, classifier)
    except (
        ConfigurationError,
        DailyQuotaExhaustedError,
        InsufficientBalanceError,
    ) as e:
        logger.warning(
            "classify_content_no_retry",
            extraction_id=ready.extraction_id,
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
                extraction_id=ready.extraction_id,
                reason=str(e),
            )
            return
        raise

    # Stage E へ chain (Pattern A': 上流 Task が下流 Ready を構築 — spec §7.1)。
    # ClassifiedOutcome の analysis から ReadyForEmbedding を構築する。
    # AlreadyClassified / Skipped Outcome は廃止 (ready 構築時点で
    # try_advance_from が None で止めるため到達しない)。
    if isinstance(result, ClassifiedOutcome):
        async with session_factory() as session:
            embedding_repo = EmbeddingRepository(session)
            ready_emb = await ReadyForEmbedding.try_advance_from(
                result.analysis,
                embedding_repo,
            )
        if ready_emb is not None:
            await generate_embedding.kiq(ready_emb)


# ---------------------------------------------------------------------------
# Embedding (Stage E)
# ---------------------------------------------------------------------------


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
    elif isinstance(result, InvalidInputOutcome):
        logger.info(
            "generate_embedding_invalid_input",
            analysis_id=ready.analysis_id,
        )
