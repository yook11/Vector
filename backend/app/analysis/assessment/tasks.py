"""Stage 4 (Assessment) taskiq タスク。

Stage 3 (extract_content) から ``AssessmentTrigger`` (extraction_id のみ運ぶ
軽量 ID キャリア) で chain される。本 task が処理開始時に
``ReadyForAssessment.try_advance_from`` を呼んで最新の DB 状態から厚い Ready を
構築する (案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。

precondition 未充足 (extraction 不在 / 既 in-scope / 既 out-of-scope) の場合は
rate limit acquire を試みず即 return する (Ready 構築が gatekeeper)。

in-scope 判定で永続化に成功した場合は ``EmbeddingTrigger`` (analysis_id のみ)
を kiq に流して Stage 5 (``generate_embedding``) を起動する。Stage 5 Ready の
構築は下流 Stage 5 task 自身が処理開始時に行う。

失敗 audit 方針 (PR4 2026-05-13): except 節は branch 固有 log と
``failure_exc`` / ``reraise`` flag 設定に専念し、共通の audit 書込み + log
fallback は task 末尾で 1 回だけ inline で実行する (Stage 3 / Stage 5 と同型)。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis._limiter_factory import _build_limiters
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.audit_repository import AssessmentAuditRepository
from app.analysis.assessment.domain.ready import (
    AssessmentTrigger,
    ReadyForAssessment,
)
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.analysis.assessment.repository import AssessmentRepository
from app.analysis.assessment.service import AssessmentService
from app.analysis.embedding.domain.ready import EmbeddingTrigger
from app.analysis.embedding.tasks import generate_embedding
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_analysis, is_last_attempt
from app.observability.redact import redact_secrets

logger = structlog.get_logger(__name__)


@broker_analysis.task(
    task_name="assess_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def assess_content(
    trigger: AssessmentTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一 extraction に対して Stage 4 (Assessment) を実行する。

    Stage 4 は in-scope / out-of-scope の判定 + 該当時の category / topic /
    investor_take 抽出を一括して行う。

    案 3 適用: 受け取った ``AssessmentTrigger`` は ``extraction_id`` のみ運ぶ
    軽量 message。本 task が処理開始時に
    ``ReadyForAssessment.try_advance_from`` を呼んで最新の DB 状態から厚い
    Ready を構築する。Ready 構築が成功 = precondition (extraction 存在 +
    未 in-scope 評価 + 未 out-of-scope 評価) が satisfy された状態。

    順序: Ready 構築 → rate limit acquire → Service.execute。precondition 未充足
    で AI quota を消費しないよう、Ready 構築を rate limit より前に置く
    (Stage 5 と対称、`feedback_failure_visibility` + 案 3 順序)。
    """
    session_factory = ctx.state.session_factory
    assessor: BaseAssessor = ctx.state.assessor

    # 処理開始時に Ready を構築 (precondition + audit 参照値の全揃え)
    async with session_factory() as session:
        ready = await ReadyForAssessment.try_advance_from(
            extraction_id=trigger.extraction_id,
            repo=AssessmentRepository(session),
        )
    if ready is None:
        logger.info(
            "assess_content_skipped",
            extraction_id=trigger.extraction_id,
            reason="precondition_not_met",
        )
        return

    # AI を呼ぶ見込みが立ってから rate limit acquire
    rpm_limiter, rpd_limiter = _build_limiters(
        "assess", assessor.MODEL, assessor.RPM, assessor.RPD
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

    failure_exc: BaseException
    reraise: bool
    try:
        result = await svc.execute(ready, assessor)
    except AssessmentTerminalSkipError as exc:
        # Layer 1 marker (Layer 2-B AssessmentCategoryMissingError も継承で拾う):
        # 永続的失敗 → audit 焼いて即 return (taskiq retry なし、extraction 保持)。
        logger.warning(
            "assess_content_terminal_skip",
            extraction_id=ready.extraction_id,
            code=getattr(exc, "code", None),
        )
        failure_exc = exc
        reraise = False
    except AssessmentRecoverableError as exc:
        # Layer 1 marker (Layer 2-B AssessmentResponseInvalidError も継承で拾う):
        # 一時的失敗 → audit 焼いて is_last_attempt でトリアージ。
        failure_exc = exc
        if is_last_attempt(ctx):
            logger.warning(
                "assess_content_recoverable_exhausted",
                extraction_id=ready.extraction_id,
                code=getattr(exc, "code", None),
            )
            reraise = False
        else:
            reraise = True  # taskiq 再試行
    except Exception as exc:
        # catch-all (想定外): audit 焼いて exhausted なら return、否則 raise。
        failure_exc = exc
        if is_last_attempt(ctx):
            logger.exception(
                "assess_content_unexpected_exhausted",
                extraction_id=ready.extraction_id,
            )
            reraise = False
        else:
            reraise = True
    else:
        # ``result`` は in-scope 成功時のみ assessment id、out-of-scope と race 敗北
        # は ``None``。out-of-scope はパイプライン終了で chain しない。race 敗北は
        # 勝者 task が自身で Stage 5 を起動する (勝者 crash 時は reconcile cron 経路、
        # 本 task の責務外)。
        if result is not None:
            # Stage 5 (Embedding) を ID で起動 (案 3: 下流 Stage 自身が処理開始時に
            # Ready を構築する)。Trigger に analysis_id だけ詰めて kiq へ enqueue する。
            await generate_embedding.kiq(EmbeddingTrigger(analysis_id=result))
        return

    # 共通 audit (best-effort, log fallback) — 失敗経路でのみ到達。
    # Repository は class API のみで tx 境界を握らないため、別 session 開閉 +
    # commit は Task 層 (本 inline ブロック) の責務。
    # audit 書込みは best-effort: DB 落ち / migration 漏れ / schema 不整合などで
    # INSERT または commit が失敗しても、業務 task を殺さないよう例外を呑んで
    # ``assessment_failure_audit_dropped`` 構造ログにフォールバックする
    # (運用シグナル、監査の audit ではない)。SDK exception message に key prefix
    # / Authorization header が混入しうるため、DB payload と同 pattern で
    # ログ経路にも ``redact_secrets`` を通す (red-team chain γ-2 対称化)。
    try:
        async with session_factory() as session:
            await AssessmentAuditRepository(session).append_failure(
                ready=ready, exc=failure_exc, attempt=attempt
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "assessment_failure_audit_dropped",
            extraction_id=ready.extraction_id,
            attempt=attempt,
            business_error_class=(
                f"{type(failure_exc).__module__}.{type(failure_exc).__qualname__}"
            ),
            business_error_message=redact_secrets(str(failure_exc))[:500],
            audit_error_class=(
                f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
            ),
            audit_error_message=redact_secrets(str(audit_exc))[:500],
        )

    if reraise:
        raise failure_exc
