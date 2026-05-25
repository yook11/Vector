"""back-fill cron タスク 3 本 (curations / assessments / embeddings)。

各タスクは broker_metadata 上で cron 駆動し、塩漬け化した記事 ID を発見して
対応するメインフロー task を ``kiq`` で再投入する。kill switch (Settings)
が False のときは即 return し、circuit breaker (連続塩漬け検出) と
日次予算 (Redis) で暴走を防ぐ。

ツマミの所在は本ファイル冒頭の定数群。env で動的に変えるのは kill switch
のみ (PLAN §3-6 / §3-9)。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.brokers import broker_metadata
from app.config import settings
from app.maintenance.backlog import PipelineBacklog
from app.maintenance.budget import consume_daily_budget
from app.maintenance.policy import BackfillWindow, utc_now
from app.redis import get_redis

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# ツマミ (env で動かすのは kill switch のみ。実行時に変えたい値はここに集約)
# ---------------------------------------------------------------------------

CURATIONS_LIMIT = 50
CURATIONS_DAILY_MAX = 600

ASSESSMENTS_LIMIT = 50
ASSESSMENTS_DAILY_MAX = 600

EMBEDDINGS_LIMIT = 50
EMBEDDINGS_DAILY_MAX = 1500

# 連続して塩漬け SELECT が空でない回数がこれを超えたら、メインフローに
# 根本的な詰まりがあると見なして本日分の back-fill を止める。
CIRCUIT_THRESHOLD = 4
_CIRCUIT_TTL_SECONDS = 6 * 60 * 60


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------


def _circuit_key(role: str) -> str:
    return f"backfill:circuit:{role}:streak"


async def _update_circuit_breaker(role: str, found: int) -> int:
    """空クエリで streak リセット、非空なら increment。現在の streak を返す。"""
    redis = get_redis()
    key = _circuit_key(role)
    if found == 0:
        await redis.delete(key)
        return 0
    streak = await redis.incr(key)
    await redis.expire(key, _CIRCUIT_TTL_SECONDS)
    return int(streak)


# ---------------------------------------------------------------------------
# Stage 2a: curation の塩漬け救済
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="backfill_curations",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": "*/15 * * * *"}],
)
async def backfill_curations(ctx: Context = TaskiqDepends()) -> None:
    """curation 子が NULL の Article を発見し curate_content を再投入する。

    案 3 適用: 本 task は ID-only な ``CurationTrigger`` を kiq に詰めて
    enqueue するだけ。precondition 判定 (Article 存在 + signal/noise 未生成 +
    本文サイズ ≤ hard cap) と Ready 構築は下流 Stage 3 task が処理開始時に
    行う (`curate_content_skipped reason=precondition_not_met` log で観測可能)。
    """
    if not settings.backfill_curations_enabled:
        logger.info("backfill_curations_disabled")
        return

    session_factory = ctx.state.session_factory
    before, after = BackfillWindow().boundaries_at(utc_now())

    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        ids = await backlog.article_ids_pending_curation(
            created_before=before,
            created_after=after,
            limit=CURATIONS_LIMIT,
        )

    found = len(ids)
    streak = await _update_circuit_breaker("curate", found)
    if streak >= CIRCUIT_THRESHOLD:
        logger.warning("backfill_curations_circuit_open", streak=streak, found=found)
        return
    if found == 0:
        logger.info("backfill_curations_empty")
        return

    granted = await consume_daily_budget(
        get_redis(), "curate", found, CURATIONS_DAILY_MAX
    )
    if granted == 0:
        logger.warning("backfill_curations_daily_budget_exhausted", found=found)
        return

    from app.analysis.curation.domain.ready import CurationTrigger
    from app.analysis.curation.tasks import curate_content

    requeued = 0
    for article_id in ids[:granted]:
        try:
            await curate_content.kiq(CurationTrigger(article_id=article_id))
            requeued += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "backfill_curations_kiq_failed",
                article_id=article_id,
                error=str(e),
            )
            continue

    # precondition_not_met (article 既消滅 / 既処理 / 本文 oversized) は
    # 下流 Stage 3 task の ``curate_content_skipped`` log で観測する
    logger.info(
        "backfill_curations_completed",
        found=found,
        granted=granted,
        requeued=requeued,
    )


# ---------------------------------------------------------------------------
# Stage 2b: assessment の塩漬け救済
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="backfill_assessments",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": "5,20,35,50 * * * *"}],
)
async def backfill_assessments(ctx: Context = TaskiqDepends()) -> None:
    """in-scope / out-of-scope assessment が無い Extraction を発見して
    assess_content を再投入する。

    案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): maintenance は
    「投入数を見る」役割に縮退し、precondition 検証 + Ready 構築は下流 Stage 4
    task に委ねる。各 curation_id を ``AssessmentTrigger`` に詰めて kiq に
    流すだけ。stale trigger (既 assess 済など) は Stage 4 task の
    ``assess_content_skipped`` ログで観測する。
    """
    if not settings.backfill_assessments_enabled:
        logger.info("backfill_assessments_disabled")
        return

    session_factory = ctx.state.session_factory
    before, after = BackfillWindow().boundaries_at(utc_now())

    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        ids = await backlog.curation_ids_pending_assessment(
            created_before=before,
            created_after=after,
            limit=ASSESSMENTS_LIMIT,
        )

    found = len(ids)
    streak = await _update_circuit_breaker("assess", found)
    if streak >= CIRCUIT_THRESHOLD:
        logger.warning("backfill_assessments_circuit_open", streak=streak, found=found)
        return
    if found == 0:
        logger.info("backfill_assessments_empty")
        return

    granted = await consume_daily_budget(
        get_redis(), "assess", found, ASSESSMENTS_DAILY_MAX
    )
    if granted == 0:
        logger.warning("backfill_assessments_daily_budget_exhausted", found=found)
        return

    from app.analysis.assessment.domain.ready import AssessmentTrigger
    from app.analysis.assessment.tasks import assess_content

    # 案 3: maintenance も上流相当 → ID のみ enqueue。precondition 検証は
    # Stage 4 Task 自身が処理開始時に行う。stale trigger は Stage 4 の
    # ``assess_content_skipped`` ログで観測する。
    requeued = 0
    for curation_id in ids[:granted]:
        try:
            await assess_content.kiq(
                AssessmentTrigger(curation_id=curation_id),
            )
            requeued += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "backfill_assessments_kiq_failed",
                curation_id=curation_id,
                error=str(e),
            )
            continue

    logger.info(
        "backfill_assessments_completed",
        found=found,
        granted=granted,
        requeued=requeued,
    )


# ---------------------------------------------------------------------------
# Stage 3: embedding の塩漬け救済
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="backfill_embeddings",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": "*/10 * * * *"}],
)
async def backfill_embeddings(ctx: Context = TaskiqDepends()) -> None:
    """embedding NULL の analysis を発見し ``generate_embedding`` を再投入する。

    案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築): maintenance は
    「投入数を見る」役割に縮退し、precondition 検証 + Ready 構築は下流 Stage 5
    task に委ねる。各 analysis_id を ``EmbeddingTrigger`` に詰めて kiq に流すだけ。
    stale trigger (既 embedded など) は Stage 5 task の
    ``generate_embedding_skipped`` ログで観測する。
    """
    if not settings.backfill_embeddings_enabled:
        logger.info("backfill_embeddings_disabled")
        return

    session_factory = ctx.state.session_factory
    before, after = BackfillWindow().boundaries_at(utc_now())

    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        ids = await backlog.analysis_ids_pending_embedding(
            created_before=before,
            created_after=after,
            limit=EMBEDDINGS_LIMIT,
        )

    found = len(ids)
    streak = await _update_circuit_breaker("embed", found)
    if streak >= CIRCUIT_THRESHOLD:
        logger.warning("backfill_embeddings_circuit_open", streak=streak, found=found)
        return
    if found == 0:
        logger.info("backfill_embeddings_empty")
        return

    granted = await consume_daily_budget(
        get_redis(), "embed", found, EMBEDDINGS_DAILY_MAX
    )
    if granted == 0:
        logger.warning("backfill_embeddings_daily_budget_exhausted", found=found)
        return

    from app.analysis.embedding.domain.ready import EmbeddingTrigger
    from app.analysis.embedding.tasks import generate_embedding

    # 案 3: maintenance も上流相当 → ID のみ enqueue。precondition 検証は
    # Stage 5 Task 自身が処理開始時に行う。stale trigger は Stage 5 の
    # ``generate_embedding_skipped`` ログで観測する。
    requeued = 0
    for assessment_id in ids[:granted]:
        try:
            await generate_embedding.kiq(
                EmbeddingTrigger(analysis_id=assessment_id),
            )
            requeued += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "backfill_embeddings_kiq_failed",
                assessment_id=assessment_id,
                error=str(e),
            )
            continue

    logger.info(
        "backfill_embeddings_completed",
        found=found,
        granted=granted,
        requeued=requeued,
    )
