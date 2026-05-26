"""Back-fill cron タスク 3 本 (curations / assessments / embeddings)。

各タスクは broker_metadata 上で cron 駆動し、塩漬け化した記事 ID を発見して
対応するメインフロー task を ``kiq`` で再投入する。kill switch (Settings)
が False のときは即 return し、circuit breaker (連続塩漬け検出) と
日次予算 (Redis) で暴走を防ぐ。

ツマミの所在は本ファイル冒頭の定数群。env で動的に変えるのは kill switch
のみ。
"""

from __future__ import annotations

from datetime import datetime

import logfire
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis.curation.hold import is_curation_held
from app.config import settings
from app.queue.brokers import broker_metadata
from app.queue.helpers.backlog import PipelineBacklog
from app.queue.helpers.budget import consume_daily_budget
from app.queue.helpers.window import BackfillWindow
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.messages.curation import CurationTrigger
from app.queue.messages.embedding import EmbeddingTrigger
from app.queue.schedule import (
    CRON_BACKFILL_ASSESSMENTS,
    CRON_BACKFILL_CURATIONS,
    CRON_BACKFILL_EMBEDDINGS,
)
from app.queue.tasks.assessment import assess_content
from app.queue.tasks.curation import curate_content
from app.queue.tasks.embedding import generate_embedding
from app.redis import get_redis
from app.shared.time import utc_now

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# ツマミ (env で動かすのは kill switch のみ。実行時に変えたい値はここに集約)
# ---------------------------------------------------------------------------

CURATIONS_LIMIT = 50
CURATIONS_DAILY_MAX = 600
# 1 run で年齢削除する記事の上限 (AI 非依存 = budget 非消費、削除負荷の頭打ち)。
CURATIONS_DELETE_LIMIT = 200

ASSESSMENTS_LIMIT = 50
ASSESSMENTS_DAILY_MAX = 600

EMBEDDINGS_LIMIT = 50
EMBEDDINGS_DAILY_MAX = 1500

# 連続して塩漬け SELECT が空でない回数がこれを超えたら、メインフローに
# 根本的な詰まりがあると見なして本日分の back-fill を止める。
CIRCUIT_THRESHOLD = 4
_CIRCUIT_TTL_SECONDS = 6 * 60 * 60


# ---------------------------------------------------------------------------
# Logfire metrics (Phase 4: 年齢削除の救済可視化)
# ---------------------------------------------------------------------------
#
# 年齢削除 (7日超 child-NULL curation の物理削除) を ``vector.curation.age_deleted``
# counter + ``vector.curation.age_delete_batch_size`` histogram で計測する。前者で
# 24h 合計の異常 spike (上流パイプライン障害の疑い)、後者で 1 cycle (15 min) の
# batch 分布 p99 を見て突発的な大量削除を検知する設計
# (``specs/logfire-stage3-rescue-dashboard.md`` §panel 3/4)。
#
# attribute は ``stage="curation"`` のみ (Phase 5+ で assessment/embedding を追加
# する場合の dimension)。``article_id`` は attribute に乗せない (cardinality 爆発
# 防止 + PII 隔離契約、test_maintenance_age_delete_metrics の capfire oracle で pin)。

_age_deleted_counter = logfire.metric_counter(
    "vector.curation.age_deleted",
    unit="1",
    description="年齢起因 (7日超 child-NULL) で物理削除された article 数",
)
_age_delete_batch_size_histogram = logfire.metric_histogram(
    "vector.curation.age_delete_batch_size",
    unit="1",
    description="1 cycle (cron) で年齢削除された article 数の分布 (p99 で spike 検知)",
)


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


async def _delete_aged_out_curations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    created_before: datetime,
) -> None:
    """通常窓から落ちた古い未処理記事を、監査を焼いてから物理削除する。

    記事ごとに 1 tx (audit INSERT → DELETE → commit)。``source_id`` の逆引きは
    Article 存在中にしか動かないため audit を先に焼く (``_drop_article`` と同規約。
    FK は ``ondelete=SET NULL`` 済で DELETE 後も監査行は残る)。AI 非依存のため
    budget は消費しない。
    """
    from app.audit.stages.curation import CurationAuditRepository
    from app.repositories.articles import ArticleRepository

    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        ids = await backlog.article_ids_aged_out_curation(
            created_before=created_before,
            limit=CURATIONS_DELETE_LIMIT,
        )

    deleted = 0
    for article_id in ids:
        async with session_factory() as session:
            await CurationAuditRepository(session).append_backfill_curation_aged_out(
                article_id=article_id
            )
            await ArticleRepository(session).delete_by_id(article_id)
            await session.commit()
        deleted += 1

    # Phase 4: 0 件 cycle も histogram に baseline として残す (平常時の分布形を
    # p99 の参照に活用)。counter 側は 0 件のときの ``add(0, ...)`` は spec 上
    # 許容されるが metric noise になるため > 0 時のみ。
    _age_delete_batch_size_histogram.record(deleted, attributes={"stage": "curation"})
    if deleted:
        _age_deleted_counter.add(deleted, attributes={"stage": "curation"})
        logger.info("backfill_curations_aged_out", deleted=deleted)


# ---------------------------------------------------------------------------
# Stage 2a: curation の塩漬け救済
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="backfill_curations",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_BACKFILL_CURATIONS}],
)
async def backfill_curations(ctx: Context = TaskiqDepends()) -> None:
    """curation 子が NULL の Article を救済する (再投入 + 年齢削除)。

    救済機構の本体。3 段階で動く:

    1. **hold gate**: terminal_keep (key/残高/config 等の provider/stage 健全性
       問題) が起きると失敗ハンドラが ``curation:hold`` を立てる。hold 中は
       confirmed に失敗する AI 呼び出しを避けるため run 全体を skip する。
       circuit breaker (件数で止まる) の差し替え。
    2. **年齢削除**: 通常窓 (``[after, before)``) から落ちた 7 日超の未処理記事は
       「分析価値なし」として監査を焼いてから物理削除する (P2 = silent loss 解消)。
    3. **通常再投入**: 窓内の child-NULL 記事を ID-only な ``CurationTrigger`` で
       kiq する。precondition 判定 / Ready 構築は下流 Stage 3 task に委ねる
       (``curate_content_skipped reason=precondition_not_met`` log で観測可能)。
    """
    if not settings.backfill_curations_enabled:
        logger.info("backfill_curations_disabled")
        return

    if await is_curation_held(get_redis()):
        logger.warning("backfill_curations_held")
        return

    session_factory = ctx.state.session_factory
    before, after = BackfillWindow().boundaries_at(utc_now())

    await _delete_aged_out_curations(session_factory, created_before=after)

    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        ids = await backlog.article_ids_pending_curation(
            created_before=before,
            created_after=after,
            limit=CURATIONS_LIMIT,
        )

    found = len(ids)
    if found == 0:
        logger.info("backfill_curations_empty")
        return

    granted = await consume_daily_budget(
        get_redis(), "curate", found, CURATIONS_DAILY_MAX
    )
    if granted == 0:
        logger.warning("backfill_curations_daily_budget_exhausted", found=found)
        return

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
    schedule=[{"cron": CRON_BACKFILL_ASSESSMENTS}],
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
# Stage 5: embedding の塩漬け救済
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="backfill_embeddings",
    timeout=120,
    max_retries=0,
    retry_on_error=False,
    schedule=[{"cron": CRON_BACKFILL_EMBEDDINGS}],
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
