"""Back-fill cron タスク 3 本 (curations / assessments / embeddings)。

各タスクは broker_metadata 上で cron 駆動し、塩漬け化した記事 ID を発見して
対応するメインフロー task を ``kiq`` で再投入する。kill switch (Settings)
が False のときは即 return し、日次予算 (Redis) で暴走を防ぐ。

ツマミの所在は本ファイル冒頭の定数群。env で動的に変えるのは kill switch
のみ。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import logfire
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.audit.domain.event import EventType, Stage
from app.audit.stages.backfill import (
    BackfillAuditRepository,
    BackfillOutcomeCode,
    BackfillStage,
    BackfillTargetKind,
)
from app.config import settings
from app.models.article_curation import ArticleCuration
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    BackfillExclusionReason,
    EmbeddingBackfillExclusion,
)
from app.models.in_scope_assessment import InScopeAssessment
from app.models.out_of_scope_assessment import OutOfScopeAssessment
from app.queue.brokers import broker_metadata
from app.queue.helpers.backlog import BackfillTarget, PipelineBacklog
from app.queue.helpers.budget import consume_daily_budget
from app.queue.helpers.stage_hold import (
    is_assessment_held,
    is_curation_held,
    is_embedding_held,
)
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

# Logfire metrics (Backfill core: 毎 tick で見る根本指標)
# ---------------------------------------------------------------------------
#
# rescue cron の current-state は低 cardinality の 4 metric に絞る。
# 詳細な失敗理由や対象 ID は pipeline_events に寄せ、Logfire attribute には
# stage / action だけを乗せる。
#
# ``backlog`` は LIMIT 付き dispatch list の長さではなく、LIMIT なし COUNT の真値。
# ``held`` は最後の cron tick 時点で stage hold 中なら 1、通常運転なら 0。
# ``dispatched`` は実際に kiq 成功した件数だけを increment。
# ``aged_out`` は年齢削除 / soft exclusion が commit できた件数だけを increment。

_backlog_gauge = logfire.metric_gauge(
    "vector.backfill.backlog",
    unit="1",
    description="backfill cron 冒頭で観測した DB 上の真の未処理件数 (stage 別)",
)
_held_gauge = logfire.metric_gauge(
    "vector.backfill.held",
    unit="1",
    description="backfill cron の最後の tick 時点で stage hold 中なら 1、通常なら 0",
)
_dispatched_counter = logfire.metric_counter(
    "vector.backfill.dispatched",
    unit="1",
    description="backfill cron が実際に kiq 成功した対象件数 (stage 別)",
)
_aged_out_counter = logfire.metric_counter(
    "vector.backfill.aged_out",
    unit="1",
    description="古すぎて通常 backfill から整理完了した対象件数 (stage/action 別)",
)


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------


def _new_backfill_run_id() -> str:
    """1 backfill run 内の item / summary 監査を束ねる ID を返す。"""
    return str(uuid4())


async def _append_backfill_item_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: Stage,
    backfill_stage: BackfillStage,
    run_id: str,
    target_kind: BackfillTargetKind,
    target: BackfillTarget,
    event_type: EventType,
    outcome_code: BackfillOutcomeCode,
    exc: BaseException | None = None,
) -> None:
    """item 単位監査を best-effort で焼く。"""
    try:
        async with session_factory() as session:
            await BackfillAuditRepository(session).append_item_event(
                stage=stage,
                event_type=event_type,
                outcome_code=outcome_code,
                backfill_stage=backfill_stage,
                run_id=run_id,
                target_kind=target_kind,
                target_id=target.target_id,
                article_id=target.article_id,
                source_name=target.source_name,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:  # noqa: BLE001
        logger.exception(
            "backfill_item_audit_dropped",
            stage=stage.value,
            backfill_stage=backfill_stage,
            outcome_code=outcome_code.value,
            target_kind=target_kind,
            target_id=target.target_id,
            audit_error_class=_exception_fqn(audit_exc),
        )


async def _append_backfill_run_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: Stage,
    backfill_stage: BackfillStage,
    run_id: str,
    event_type: EventType,
    outcome_code: BackfillOutcomeCode,
    selected_count: int | None = None,
    granted_count: int | None = None,
    enqueued_count: int | None = None,
    failed_count: int | None = None,
    limit: int | None = None,
    daily_max: int | None = None,
    exc: BaseException | None = None,
) -> None:
    """run 単位監査を best-effort で焼く。"""
    try:
        async with session_factory() as session:
            await BackfillAuditRepository(session).append_run_event(
                stage=stage,
                event_type=event_type,
                outcome_code=outcome_code,
                backfill_stage=backfill_stage,
                run_id=run_id,
                selected_count=selected_count,
                granted_count=granted_count,
                enqueued_count=enqueued_count,
                failed_count=failed_count,
                limit=limit,
                daily_max=daily_max,
                exc=exc,
            )
            await session.commit()
    except Exception as audit_exc:  # noqa: BLE001
        logger.exception(
            "backfill_run_audit_dropped",
            stage=stage.value,
            backfill_stage=backfill_stage,
            outcome_code=outcome_code.value,
            audit_error_class=_exception_fqn(audit_exc),
        )


def _exception_fqn(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def _record_hold_state(stage: str, *, held: bool) -> None:
    """hold gate の現在状態を low-cardinality gauge に記録する。"""
    _held_gauge.set(1 if held else 0, attributes={"stage": stage})


def _record_dispatched(stage: str, count: int) -> None:
    """実際に broker enqueue できた件数だけを記録する。"""
    if count:
        _dispatched_counter.add(count, attributes={"stage": stage})


def _record_aged_out(stage: str, *, action: str, count: int) -> None:
    """通常 backfill から整理完了した件数だけを記録する。"""
    if count:
        _aged_out_counter.add(count, attributes={"stage": stage, "action": action})


async def _delete_aged_out_curations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    created_before: datetime,
) -> int:
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
    return deleted


async def _exclude_aged_out_assessments(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    created_before: datetime,
) -> int:
    """通常窓から落ちた未 assessment curation を backfill 対象外にする。

    Stage 4 は curation という保全価値のある部分結果を持つため、Stage 3 のように
    article を物理削除せず、current-state sentinel と audit を同一 tx で残す。
    """
    from app.audit.stages.assessment import AssessmentAuditRepository

    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        ids = await backlog.curation_ids_aged_out_assessment(
            created_before=created_before,
            limit=ASSESSMENTS_LIMIT,
        )

    excluded = 0
    for curation_id in ids:
        async with session_factory() as session:
            stmt = (
                select(ArticleCuration.article_id)
                .outerjoin(
                    InScopeAssessment,
                    InScopeAssessment.curation_id == ArticleCuration.id,
                )
                .outerjoin(
                    OutOfScopeAssessment,
                    OutOfScopeAssessment.curation_id == ArticleCuration.id,
                )
                .outerjoin(
                    AssessmentBackfillExclusion,
                    AssessmentBackfillExclusion.curation_id == ArticleCuration.id,
                )
                .where(
                    ArticleCuration.id == curation_id,
                    InScopeAssessment.id.is_(None),
                    OutOfScopeAssessment.id.is_(None),
                    AssessmentBackfillExclusion.curation_id.is_(None),
                )
                .limit(1)
            )
            article_id = await session.scalar(stmt)
            if article_id is None:
                continue

            session.add(
                AssessmentBackfillExclusion(
                    curation_id=curation_id,
                    reason_code=BackfillExclusionReason.ASSESSMENT_AGED_OUT.value,
                )
            )
            try:
                await AssessmentAuditRepository(
                    session
                ).append_backfill_assessment_aged_out(
                    curation_id=curation_id,
                    article_id=article_id,
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                continue
        excluded += 1

    if excluded:
        logger.info("backfill_assessments_aged_out_excluded", excluded=excluded)
    return excluded


async def _exclude_aged_out_embeddings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    created_before: datetime,
) -> int:
    """通常窓から落ちた embedding NULL analysis を backfill 対象外にする。"""
    from app.audit.stages.embedding import EmbeddingAuditRepository

    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        ids = await backlog.analysis_ids_aged_out_embedding(
            created_before=created_before,
            limit=EMBEDDINGS_LIMIT,
        )

    excluded = 0
    for analysis_id in ids:
        async with session_factory() as session:
            stmt = (
                select(ArticleCuration.article_id)
                .select_from(InScopeAssessment)
                .join(
                    ArticleCuration,
                    ArticleCuration.id == InScopeAssessment.curation_id,
                )
                .outerjoin(
                    EmbeddingBackfillExclusion,
                    EmbeddingBackfillExclusion.analysis_id == InScopeAssessment.id,
                )
                .where(
                    InScopeAssessment.id == analysis_id,
                    InScopeAssessment.embedding.is_(None),
                    EmbeddingBackfillExclusion.analysis_id.is_(None),
                )
                .limit(1)
            )
            article_id = await session.scalar(stmt)
            if article_id is None:
                continue

            session.add(
                EmbeddingBackfillExclusion(
                    analysis_id=analysis_id,
                    reason_code=BackfillExclusionReason.EMBEDDING_AGED_OUT.value,
                )
            )
            try:
                await EmbeddingAuditRepository(
                    session
                ).append_backfill_embedding_aged_out(
                    analysis_id=analysis_id,
                    article_id=article_id,
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                continue
        excluded += 1

    if excluded:
        logger.info("backfill_embeddings_aged_out_excluded", excluded=excluded)
    return excluded


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
    2. **年齢削除**: 通常窓 (``[after, before)``) から落ちた 7 日超の未処理記事は
       「分析価値なし」として監査を焼いてから物理削除する (P2 = silent loss 解消)。
    3. **通常再投入**: 窓内の child-NULL 記事を ID-only な ``CurationTrigger`` で
       kiq する。precondition 判定 / Ready 構築は下流 Stage 3 task に委ねる
       (Ready build blocked audit で観測可能)。
    """
    session_factory = ctx.state.session_factory
    run_id = _new_backfill_run_id()
    if not settings.backfill_curations_enabled:
        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_CURATE,
            backfill_stage="curate",
            run_id=run_id,
            event_type=EventType.SKIPPED,
            outcome_code=BackfillOutcomeCode.RUN_KILL_SWITCH_DISABLED,
            limit=CURATIONS_LIMIT,
            daily_max=CURATIONS_DAILY_MAX,
        )
        logger.info("backfill_curations_disabled")
        return

    try:
        curation_held = await is_curation_held(get_redis())
        _record_hold_state("curation", held=curation_held)
        if curation_held:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_CURATE,
                backfill_stage="curate",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_HELD_BY_STAGE_HOLD,
                limit=CURATIONS_LIMIT,
                daily_max=CURATIONS_DAILY_MAX,
            )
            logger.warning("backfill_curations_held")
            return

        before, after = BackfillWindow().boundaries_at(utc_now())

        aged_out_count = await _delete_aged_out_curations(
            session_factory, created_before=after
        )
        _record_aged_out("curation", action="deleted", count=aged_out_count)

        async with session_factory() as session:
            backlog = PipelineBacklog(session)
            backlog_count = await backlog.count_articles_pending_curation(
                created_before=before,
                created_after=after,
            )
            targets = await backlog.curation_targets_pending(
                created_before=before,
                created_after=after,
                limit=CURATIONS_LIMIT,
            )

        _backlog_gauge.set(backlog_count, attributes={"stage": "curation"})

        found = len(targets)
        if found == 0:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_CURATE,
                backfill_stage="curate",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_NO_TARGETS,
                selected_count=0,
                granted_count=0,
                enqueued_count=0,
                failed_count=0,
                limit=CURATIONS_LIMIT,
                daily_max=CURATIONS_DAILY_MAX,
            )
            logger.info("backfill_curations_empty")
            return

        granted = await consume_daily_budget(
            get_redis(), "curate", found, CURATIONS_DAILY_MAX
        )
        if granted == 0:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_CURATE,
                backfill_stage="curate",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_DAILY_BUDGET_EXHAUSTED,
                selected_count=found,
                granted_count=0,
                enqueued_count=0,
                failed_count=0,
                limit=CURATIONS_LIMIT,
                daily_max=CURATIONS_DAILY_MAX,
            )
            logger.warning("backfill_curations_daily_budget_exhausted", found=found)
            return

        enqueued = 0
        failed = 0
        for target in targets[:granted]:
            try:
                await curate_content.kiq(CurationTrigger(article_id=target.target_id))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                await _append_backfill_item_event(
                    session_factory,
                    stage=Stage.BACKFILL_CURATE,
                    backfill_stage="curate",
                    run_id=run_id,
                    target_kind="article",
                    target=target,
                    event_type=EventType.FAILED,
                    outcome_code=BackfillOutcomeCode.ITEM_ENQUEUE_FAILED,
                    exc=exc,
                )
                logger.warning(
                    "backfill_curations_kiq_failed",
                    article_id=target.target_id,
                    error=str(exc),
                )
                continue

            enqueued += 1
            await _append_backfill_item_event(
                session_factory,
                stage=Stage.BACKFILL_CURATE,
                backfill_stage="curate",
                run_id=run_id,
                target_kind="article",
                target=target,
                event_type=EventType.SUCCEEDED,
                outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
            )

        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_CURATE,
            backfill_stage="curate",
            run_id=run_id,
            event_type=EventType.SUCCEEDED,
            outcome_code=BackfillOutcomeCode.RUN_COMPLETED,
            selected_count=found,
            granted_count=granted,
            enqueued_count=enqueued,
            failed_count=failed,
            limit=CURATIONS_LIMIT,
            daily_max=CURATIONS_DAILY_MAX,
        )
        _record_dispatched("curation", enqueued)
    except Exception as exc:
        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_CURATE,
            backfill_stage="curate",
            run_id=run_id,
            event_type=EventType.FAILED,
            outcome_code=BackfillOutcomeCode.RUN_FAILED,
            limit=CURATIONS_LIMIT,
            daily_max=CURATIONS_DAILY_MAX,
            exc=exc,
        )
        raise

    # article 既消滅 / 既処理 / 本文 oversized は、下流 Stage 3 task の
    # Ready build blocked audit で観測する
    logger.info(
        "backfill_curations_completed",
        found=found,
        granted=granted,
        requeued=enqueued,
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
    流すだけ。通常窓から落ちた未 assessment curation は、保全価値のある部分結果
    を残すため削除せず ``assessment_backfill_exclusions`` に current-state
    sentinel を作る。stale trigger (既 assess 済など) は Stage 4 task の
    ``assess_content_skipped`` ログで観測する。
    """
    session_factory = ctx.state.session_factory
    run_id = _new_backfill_run_id()
    if not settings.backfill_assessments_enabled:
        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_ASSESS,
            backfill_stage="assess",
            run_id=run_id,
            event_type=EventType.SKIPPED,
            outcome_code=BackfillOutcomeCode.RUN_KILL_SWITCH_DISABLED,
            limit=ASSESSMENTS_LIMIT,
            daily_max=ASSESSMENTS_DAILY_MAX,
        )
        logger.info("backfill_assessments_disabled")
        return

    try:
        assessment_held = await is_assessment_held(get_redis())
        _record_hold_state("assessment", held=assessment_held)
        if assessment_held:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_ASSESS,
                backfill_stage="assess",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_HELD_BY_STAGE_HOLD,
                limit=ASSESSMENTS_LIMIT,
                daily_max=ASSESSMENTS_DAILY_MAX,
            )
            logger.warning("backfill_assessments_held")
            return

        before, after = BackfillWindow().boundaries_at(utc_now())

        aged_out_count = await _exclude_aged_out_assessments(
            session_factory, created_before=after
        )
        _record_aged_out("assessment", action="excluded", count=aged_out_count)

        async with session_factory() as session:
            backlog = PipelineBacklog(session)
            # 観測 (COUNT) → dispatch (target 取得) の順で同一 session 内に並べ、
            # read committed snapshot 上で一貫値を返す。
            backlog_count = await backlog.count_curations_pending_assessment(
                created_before=before,
                created_after=after,
            )
            targets = await backlog.assessment_targets_pending(
                created_before=before,
                created_after=after,
                limit=ASSESSMENTS_LIMIT,
            )

        _backlog_gauge.set(backlog_count, attributes={"stage": "assessment"})

        found = len(targets)
        if found == 0:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_ASSESS,
                backfill_stage="assess",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_NO_TARGETS,
                selected_count=0,
                granted_count=0,
                enqueued_count=0,
                failed_count=0,
                limit=ASSESSMENTS_LIMIT,
                daily_max=ASSESSMENTS_DAILY_MAX,
            )
            logger.info("backfill_assessments_empty")
            return

        granted = await consume_daily_budget(
            get_redis(), "assess", found, ASSESSMENTS_DAILY_MAX
        )
        if granted == 0:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_ASSESS,
                backfill_stage="assess",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_DAILY_BUDGET_EXHAUSTED,
                selected_count=found,
                granted_count=0,
                enqueued_count=0,
                failed_count=0,
                limit=ASSESSMENTS_LIMIT,
                daily_max=ASSESSMENTS_DAILY_MAX,
            )
            logger.warning("backfill_assessments_daily_budget_exhausted", found=found)
            return

        # 案 3: maintenance も上流相当 → ID のみ enqueue。precondition 検証は
        # Stage 4 Task 自身が処理開始時に行う。stale trigger は Stage 4 の
        # ``assess_content_skipped`` ログで観測する。
        enqueued = 0
        failed = 0
        for target in targets[:granted]:
            try:
                await assess_content.kiq(
                    AssessmentTrigger(curation_id=target.target_id),
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                await _append_backfill_item_event(
                    session_factory,
                    stage=Stage.BACKFILL_ASSESS,
                    backfill_stage="assess",
                    run_id=run_id,
                    target_kind="curation",
                    target=target,
                    event_type=EventType.FAILED,
                    outcome_code=BackfillOutcomeCode.ITEM_ENQUEUE_FAILED,
                    exc=exc,
                )
                logger.warning(
                    "backfill_assessments_kiq_failed",
                    curation_id=target.target_id,
                    error=str(exc),
                )
                continue

            enqueued += 1
            await _append_backfill_item_event(
                session_factory,
                stage=Stage.BACKFILL_ASSESS,
                backfill_stage="assess",
                run_id=run_id,
                target_kind="curation",
                target=target,
                event_type=EventType.SUCCEEDED,
                outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
            )

        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_ASSESS,
            backfill_stage="assess",
            run_id=run_id,
            event_type=EventType.SUCCEEDED,
            outcome_code=BackfillOutcomeCode.RUN_COMPLETED,
            selected_count=found,
            granted_count=granted,
            enqueued_count=enqueued,
            failed_count=failed,
            limit=ASSESSMENTS_LIMIT,
            daily_max=ASSESSMENTS_DAILY_MAX,
        )
        _record_dispatched("assessment", enqueued)
    except Exception as exc:
        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_ASSESS,
            backfill_stage="assess",
            run_id=run_id,
            event_type=EventType.FAILED,
            outcome_code=BackfillOutcomeCode.RUN_FAILED,
            limit=ASSESSMENTS_LIMIT,
            daily_max=ASSESSMENTS_DAILY_MAX,
            exc=exc,
        )
        raise

    logger.info(
        "backfill_assessments_completed",
        found=found,
        granted=granted,
        requeued=enqueued,
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
    session_factory = ctx.state.session_factory
    run_id = _new_backfill_run_id()
    if not settings.backfill_embeddings_enabled:
        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_EMBED,
            backfill_stage="embed",
            run_id=run_id,
            event_type=EventType.SKIPPED,
            outcome_code=BackfillOutcomeCode.RUN_KILL_SWITCH_DISABLED,
            limit=EMBEDDINGS_LIMIT,
            daily_max=EMBEDDINGS_DAILY_MAX,
        )
        logger.info("backfill_embeddings_disabled")
        return

    try:
        embedding_held = await is_embedding_held(get_redis())
        _record_hold_state("embedding", held=embedding_held)
        if embedding_held:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_EMBED,
                backfill_stage="embed",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_HELD_BY_STAGE_HOLD,
                limit=EMBEDDINGS_LIMIT,
                daily_max=EMBEDDINGS_DAILY_MAX,
            )
            logger.warning("backfill_embeddings_held")
            return

        before, after = BackfillWindow().boundaries_at(utc_now())

        aged_out_count = await _exclude_aged_out_embeddings(
            session_factory, created_before=after
        )
        _record_aged_out("embedding", action="excluded", count=aged_out_count)

        async with session_factory() as session:
            backlog = PipelineBacklog(session)
            backlog_count = await backlog.count_analyses_pending_embedding(
                created_before=before,
                created_after=after,
            )
            targets = await backlog.embedding_targets_pending(
                created_before=before,
                created_after=after,
                limit=EMBEDDINGS_LIMIT,
            )

        _backlog_gauge.set(backlog_count, attributes={"stage": "embedding"})

        found = len(targets)
        if found == 0:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_EMBED,
                backfill_stage="embed",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_NO_TARGETS,
                selected_count=0,
                granted_count=0,
                enqueued_count=0,
                failed_count=0,
                limit=EMBEDDINGS_LIMIT,
                daily_max=EMBEDDINGS_DAILY_MAX,
            )
            logger.info("backfill_embeddings_empty")
            return

        granted = await consume_daily_budget(
            get_redis(), "embed", found, EMBEDDINGS_DAILY_MAX
        )
        if granted == 0:
            await _append_backfill_run_event(
                session_factory,
                stage=Stage.BACKFILL_EMBED,
                backfill_stage="embed",
                run_id=run_id,
                event_type=EventType.SKIPPED,
                outcome_code=BackfillOutcomeCode.RUN_DAILY_BUDGET_EXHAUSTED,
                selected_count=found,
                granted_count=0,
                enqueued_count=0,
                failed_count=0,
                limit=EMBEDDINGS_LIMIT,
                daily_max=EMBEDDINGS_DAILY_MAX,
            )
            logger.warning("backfill_embeddings_daily_budget_exhausted", found=found)
            return

        # 案 3: maintenance も上流相当 → ID のみ enqueue。precondition 検証は
        # Stage 5 Task 自身が処理開始時に行う。stale trigger は Stage 5 の
        # ``generate_embedding_skipped`` ログで観測する。
        enqueued = 0
        failed = 0
        for target in targets[:granted]:
            try:
                await generate_embedding.kiq(
                    EmbeddingTrigger(analysis_id=target.target_id),
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                await _append_backfill_item_event(
                    session_factory,
                    stage=Stage.BACKFILL_EMBED,
                    backfill_stage="embed",
                    run_id=run_id,
                    target_kind="analysis",
                    target=target,
                    event_type=EventType.FAILED,
                    outcome_code=BackfillOutcomeCode.ITEM_ENQUEUE_FAILED,
                    exc=exc,
                )
                logger.warning(
                    "backfill_embeddings_kiq_failed",
                    analysis_id=target.target_id,
                    error=str(exc),
                )
                continue

            enqueued += 1
            await _append_backfill_item_event(
                session_factory,
                stage=Stage.BACKFILL_EMBED,
                backfill_stage="embed",
                run_id=run_id,
                target_kind="analysis",
                target=target,
                event_type=EventType.SUCCEEDED,
                outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
            )

        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_EMBED,
            backfill_stage="embed",
            run_id=run_id,
            event_type=EventType.SUCCEEDED,
            outcome_code=BackfillOutcomeCode.RUN_COMPLETED,
            selected_count=found,
            granted_count=granted,
            enqueued_count=enqueued,
            failed_count=failed,
            limit=EMBEDDINGS_LIMIT,
            daily_max=EMBEDDINGS_DAILY_MAX,
        )
        _record_dispatched("embedding", enqueued)
    except Exception as exc:
        await _append_backfill_run_event(
            session_factory,
            stage=Stage.BACKFILL_EMBED,
            backfill_stage="embed",
            run_id=run_id,
            event_type=EventType.FAILED,
            outcome_code=BackfillOutcomeCode.RUN_FAILED,
            limit=EMBEDDINGS_LIMIT,
            daily_max=EMBEDDINGS_DAILY_MAX,
            exc=exc,
        )
        raise

    logger.info(
        "backfill_embeddings_completed",
        found=found,
        granted=granted,
        requeued=enqueued,
    )
