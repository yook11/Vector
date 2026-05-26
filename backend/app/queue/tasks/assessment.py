"""Stage 4 (Assessment) taskiq タスク。

Stage 3 (curate_content) から ``AssessmentTrigger`` (curation_id のみ運ぶ
軽量 ID キャリア) で chain される。本 task が処理開始時に
``ReadyForAssessment.try_advance_from`` を呼んで最新の DB 状態から厚い Ready を
構築する (案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。

precondition 未充足 (curation 不在 / 既 in-scope / 既 out-of-scope) の場合は
rate limit acquire を試みず即 return する (Ready 構築が gatekeeper)。

in-scope 判定で永続化に成功した場合は ``EmbeddingTrigger`` (analysis_id のみ)
を kiq に流して Stage 5 (``generate_embedding``) を起動する。Stage 5 Ready の
構築は下流 Stage 5 task 自身が処理開始時に行う。

失敗 dispatch / audit は ``AssessmentFailureHandler`` (``failure_handling.py``)
に委譲する。Task 層は marker の意味を持たず、Handler の戻り値 (``reraise: bool``)
だけを解釈して taskiq の raise / return semantics に変換する (Stage 3 と同型)。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.assessment.failure_handling import AssessmentFailureHandler
from app.analysis.assessment.repository import AssessmentRepository
from app.analysis.assessment.service import AssessmentService
from app.queue.brokers import broker_analysis
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.embedding import EmbeddingTrigger
from app.queue.retry import is_last_attempt
from app.queue.tasks.embedding import generate_embedding

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
    """単一 curation に対して Stage 4 (Assessment) を実行する。

    Stage 4 は in-scope / out-of-scope の判定 + 該当時の category / topic /
    investor_take 抽出を一括して行う。

    案 3 適用: 受け取った ``AssessmentTrigger`` は ``curation_id`` のみ運ぶ
    軽量 message。本 task が処理開始時に
    ``ReadyForAssessment.try_advance_from`` を呼んで最新の DB 状態から厚い
    Ready を構築する。Ready 構築が成功 = precondition (curation 存在 +
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
            curation_id=trigger.curation_id,
            repo=AssessmentRepository(session),
        )
    if ready is None:
        logger.info(
            "assess_content_skipped",
            curation_id=trigger.curation_id,
            reason="precondition_not_met",
        )
        return

    # AI を呼ぶ見込みが立ってから rate limit acquire (Stage 3 / Stage 5 と対称)
    if not await ctx.state.provider_rate_limit_gate.acquire(assessor.rate_policy):
        logger.warning(
            "assess_content_daily_quota",
            curation_id=ready.curation_id,
        )
        return

    svc = AssessmentService(session_factory)
    handler = AssessmentFailureHandler(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1

    try:
        result = await svc.execute(ready, assessor)
    except Exception as exc:
        reraise = await handler.handle(
            ready=ready,
            exc=exc,
            attempt=attempt,
            last_attempt=is_last_attempt(ctx),
        )
        if reraise:
            raise
        return

    # ``result`` は in-scope 成功時のみ assessment id、out-of-scope と race 敗北
    # は ``None``。out-of-scope はパイプライン終了で chain しない。race 敗北は
    # 勝者 task が自身で Stage 5 を起動する (勝者 crash 時は reconcile cron 経路、
    # 本 task の責務外)。
    if result is not None:
        # Stage 5 (Embedding) を ID で起動 (案 3: 下流 Stage 自身が処理開始時に
        # Ready を構築する)。Trigger に analysis_id だけ詰めて kiq へ enqueue する。
        await generate_embedding.kiq(EmbeddingTrigger(analysis_id=result))
