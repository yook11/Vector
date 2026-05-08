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
)
from app.analysis.embedding.service import (
    InvalidInputOutcome as EmbeddingInvalidInputOutcome,
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
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.failure_recording import record_extraction_failure
from app.analysis.extraction.service import (
    ExtractedOutcome,
    ExtractionService,
    NoiseOutcome,
)
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_analysis, broker_embedding, is_last_attempt
from app.observability.categories import (
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)

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

    # Stage 4 へ chain (Pattern A': 上流 Task が下流 Ready を構築 — spec §7.1)
    if isinstance(result, ExtractedOutcome):
        async with session_factory() as session:
            analysis_repo = AnalysisRepository(session)
            rejection_repo = RejectionRepository(session)
            ready_class = await ReadyForClassification.try_advance_from(
                result.extraction,
                analysis_repo=analysis_repo,
                rejection_repo=rejection_repo,
            )
        if ready_class is not None:
            await classify_content.kiq(ready_class)
    elif isinstance(result, NoiseOutcome):
        logger.info(
            "extract_content_noise",
            article_id=ready.article_id,
        )


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
    elif isinstance(result, EmbeddingInvalidInputOutcome):
        logger.info(
            "generate_embedding_invalid_input",
            analysis_id=ready.analysis_id,
        )
