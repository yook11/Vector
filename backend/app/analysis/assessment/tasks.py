"""Stage 4 (Assessment) taskiq タスク。

extract_content から chain され、in-scope と判定された場合は
generate_embedding (Stage E) へ chain する。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis._limiter_factory import _build_limiters
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.domain.in_scope import InScopeAssessment
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.analysis.assessment.failure_recording import record_assessment_failure
from app.analysis.assessment.service import AssessmentService
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.tasks import generate_embedding
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_analysis, is_last_attempt

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="assess_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def assess_content(
    ready: ReadyForAssessment,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一 extraction に対して Stage 4 (Assessment) を実行する。

    Stage 4 は in-scope / out-of-scope の判定 + 該当時の category / topic /
    investor_take 抽出を一括して行う。

    Pattern A': 受け取った Ready 型は precondition (extraction 存在 +
    未 in-scope 評価 + 未 out-of-scope 評価) を構造保証している。本 task は
    再 fetch / None check を行わない。
    """
    session_factory = ctx.state.session_factory
    assessor: BaseAssessor = ctx.state.assessor

    # Rate limit acquire は呼び出し側の責任
    # 注: role 文字列 "classify" は Redis 上の rate limit カウンタのキーに含まれる
    # ため、in-flight の counter を引き継ぐべく旧 role 名を据え置く。
    # 実 role 切替は PR-3 (永続境界 rename) で扱う。
    rpm_limiter, rpd_limiter = _build_limiters(
        "classify", assessor.MODEL, assessor.RPM, assessor.RPD
    )
    try:
        if rpd_limiter is not None:
            await rpd_limiter.acquire()
        if rpm_limiter is not None:
            await rpm_limiter.acquire()
    except _RateLimitExceededError:
        logger.warning(
            "assess_content_daily_quota",
            extraction_id=ready.extraction_id,
        )
        return

    # Service 呼び出し（session は内部で管理）
    svc = AssessmentService(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1
    try:
        result = await svc.execute(ready, assessor)
    except AssessmentTerminalSkipError as exc:
        # Layer 1 marker (Layer 2-B AssessmentCategoryMissingError も継承で拾う):
        # 永続的失敗 → audit 焼いて即 return (taskiq retry なし、extraction 保持)。
        await record_assessment_failure(
            session_factory, ready=ready, exc=exc, attempt=attempt
        )
        logger.warning(
            "assess_content_terminal_skip",
            extraction_id=ready.extraction_id,
            code=getattr(exc, "code", None),
        )
        return
    except AssessmentRecoverableError as exc:
        # Layer 1 marker (Layer 2-B AssessmentResponseInvalidError も継承で拾う):
        # 一時的失敗 → audit 焼いて is_last_attempt でトリアージ。
        await record_assessment_failure(
            session_factory, ready=ready, exc=exc, attempt=attempt
        )
        if is_last_attempt(ctx):
            logger.warning(
                "assess_content_recoverable_exhausted",
                extraction_id=ready.extraction_id,
                code=getattr(exc, "code", None),
            )
            return
        raise  # taskiq 再試行
    except Exception as exc:
        # catch-all (想定外): audit 焼いて exhausted なら return、否則 raise。
        await record_assessment_failure(
            session_factory, ready=ready, exc=exc, attempt=attempt
        )
        if is_last_attempt(ctx):
            logger.exception(
                "assess_content_unexpected_exhausted",
                extraction_id=ready.extraction_id,
            )
            return
        raise

    # Stage 5 (Embedding) へ chain (Pattern A': 上流 Task が下流 Ready を構築)。
    # InScopeAssessment Entity から ReadyForEmbedding を構築する。
    # OutOfScopeAssessment は chain しない (パイプライン終了)。
    if isinstance(result, InScopeAssessment):
        async with session_factory() as session:
            embedding_repo = EmbeddingRepository(session)
            ready_emb = await ReadyForEmbedding.try_advance_from(
                result,
                embedding_repo,
            )
        if ready_emb is not None:
            await generate_embedding.kiq(ready_emb)
