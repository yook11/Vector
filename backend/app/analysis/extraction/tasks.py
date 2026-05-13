"""Stage 3 (Extraction) taskiq タスク。

collection 経由で起動され、抽出成功時は assess_content (Stage 4) へ chain する。

案 3 (project_typed_pipeline_preconditions 2026-05-11 確定): 上流は ID-only な
``ExtractionTrigger`` を kiq に流し、本 task が処理開始時に
``ReadyForExtraction.try_advance_from`` を呼んで最新の DB 状態から厚い Ready を
構築する (Stage 4 / Stage 5 と完全同型)。

失敗 audit 方針 (PR4 2026-05-13): except 節は branch 固有 log と
``failure_exc`` / ``reraise`` flag 設定に専念し、共通の audit 書込み + log
fallback は task 末尾で 1 回だけ inline で実行する (Stage 4 / Stage 5 と同型)。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis._limiter_factory import _build_limiters
from app.analysis.assessment.domain.ready import AssessmentTrigger
from app.analysis.assessment.tasks import assess_content
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.audit_repository import ExtractionAuditRepository
from app.analysis.extraction.domain.ready import (
    ExtractionTrigger,
    ReadyForExtraction,
)
from app.analysis.extraction.repository import ExtractionRepository
from app.analysis.extraction.service import ExtractionService
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_analysis, is_last_attempt
from app.observability.categories import (
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)
from app.observability.redact import redact_secrets

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

    案 3 適用: 受け取った ``ExtractionTrigger`` は ``article_id`` のみ運ぶ
    軽量 message。本 task が処理開始時に
    ``ReadyForExtraction.try_advance_from`` を呼んで最新の DB 状態から厚い
    Ready を構築する。Ready 構築が成功 = precondition (article 存在 +
    signal/noise 未生成 + 本文サイズ ≤ hard cap) が satisfy された状態。

    順序: Ready 構築 → rate limit acquire → Service.execute。precondition 未充足
    で AI quota を消費しないよう、Ready 構築を rate limit より前に置く
    (Stage 4 / Stage 5 と対称、`feedback_failure_visibility`)。

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

    failure_exc: BaseException
    reraise: bool
    try:
        result = await svc.execute(ready, extractor)
    except NonRetryableDropArticle as exc:
        # 内容起因 Permanent: Service 自身が audit + DELETE を
        # mark_article_unprocessable で完結する
        # (別経路の専用 audit、共通末尾 audit には載せない)。
        await svc.mark_article_unprocessable(
            ready.article_id,
            ready.original_content,
            code=getattr(type(exc), "CODE", "ai_error_unknown_drop"),
            exc=exc,
            extractor=extractor,
        )
        return
    except NonRetryableKeepArticle as exc:
        # 記事保持 Permanent: 末尾で audit 焼いて return。
        failure_exc = exc
        reraise = False
    except RetryableError as exc:
        # INLINE_RETRY=True かつ retry 余地ありなら raise (taskiq 即時 retry)。
        # それ以外は audit + return (cron 救済委譲)。
        failure_exc = exc
        reraise = type(exc).INLINE_RETRY and not is_last_attempt(ctx)
    except Exception as exc:
        # catch-all (UNKNOWN ラベル): audit + return (cron TTL 削除に委譲)。
        failure_exc = exc
        reraise = False
    else:
        # Stage 4 を ID で起動 (案 3: 下流 Stage 自身が処理開始時に Ready を構築)。
        # Service.execute は signal 勝者のみ extraction_id を返し、noise 勝者 / race
        # 敗北は None。Stage 4 ``assessment/tasks.py`` の chain firing と同型。
        if result is not None:
            await assess_content.kiq(AssessmentTrigger(extraction_id=result))
        return

    # 共通 audit (best-effort, log fallback) — 失敗経路でのみ到達。
    # Repository は class API のみで tx 境界を握らないため、別 session 開閉 +
    # commit は Task 層 (本 inline ブロック) の責務。
    # audit 書込みは best-effort: DB 落ち / migration 漏れ / schema 不整合などで
    # INSERT または commit が失敗しても、業務 task を殺さないよう例外を呑んで
    # ``extraction_failure_audit_dropped`` 構造ログにフォールバックする
    # (運用シグナル、監査の audit ではない)。SDK exception message に key prefix
    # / Authorization header が混入しうるため、DB payload と同 pattern で
    # ログ経路にも ``redact_secrets`` を通す (red-team chain γ-2 対称化)。
    try:
        async with session_factory() as session:
            await ExtractionAuditRepository(session).append_failure(
                ready=ready,
                exc=failure_exc,
                attempt=attempt,
                extractor=extractor,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "extraction_failure_audit_dropped",
            article_id=ready.article_id,
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
