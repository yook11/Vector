"""工程別に期限切れを整理し、未完了記事のイベントを再投入する。"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

import structlog

from app.audit.domain.event import EventType
from app.audit.stages.backfill import (
    BackfillAuditRepository,
    BackfillOutcomeCode,
    BackfillStage,
    BackfillTargetKind,
)
from app.backfill.audit import append_backfill_item_event, append_backfill_run_event
from app.backfill.cleanup import (
    delete_aged_out_curations,
    exclude_aged_out_assessments,
    exclude_aged_out_embeddings,
)
from app.backfill.metrics import backlog_gauge, record_aged_out, record_dispatched
from app.backfill.policy import (
    ASSESSMENTS_LIMIT,
    CURATIONS_LIMIT,
    EMBEDDINGS_LIMIT,
    BackfillWindow,
)
from app.backfill.repository import PipelineBacklog
from app.backfill.targets import BackfillEventTarget
from app.db.session import SessionFactory
from app.logfire.stage_span import pipeline_stage_span
from app.outbox.publishing.event_batch import MAX_BATCH_MESSAGES
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventEnvelope,
    EventPublisher,
    PublishFailed,
    PublishSucceeded,
)

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _run(
    session_factory: SessionFactory,
    *,
    backfill_stage: BackfillStage,
    op: str,
) -> AsyncIterator[str]:
    """実行の失敗監査を残し、元の例外を呼び出し元へ返す。"""
    run_id = str(uuid4())
    with pipeline_stage_span(BackfillAuditRepository.stage_for(backfill_stage), op=op):
        try:
            yield run_id
        except Exception as exc:
            await append_backfill_run_event(
                session_factory,
                backfill_stage=backfill_stage,
                run_id=run_id,
                event_type=EventType.FAILED,
                outcome_code=BackfillOutcomeCode.RUN_FAILED,
                exc=exc,
            )
            raise


async def _dispatch(
    session_factory: SessionFactory,
    publisher: EventPublisher,
    targets: Sequence[BackfillEventTarget],
    *,
    backfill_stage: BackfillStage,
    stage: str,
    target_kind: BackfillTargetKind,
    run_id: str,
) -> None:
    """受付結果を対象と照合し、部分失敗でも次のバッチを送信する。"""
    enqueued = 0
    for offset in range(0, len(targets), MAX_BATCH_MESSAGES):
        batch_targets = targets[offset : offset + MAX_BATCH_MESSAGES]
        envelopes = tuple(
            EventEnvelope(
                event_id=uuid4(),
                event_type=item.payload.EVENT_TYPE,
                schema_version=item.payload.SCHEMA_VERSION,
                occurred_at=item.occurred_at,
                payload=item.payload.model_dump(mode="json"),
            )
            for item in batch_targets
        )
        batch = publisher.publish_batch(envelopes)
        if not isinstance(batch, BatchPublishResult):
            raise TypeError("publisher must return BatchPublishResult")
        if tuple(result.event_id for result in batch.results) != tuple(
            envelope.event_id for envelope in envelopes
        ):
            raise ValueError("publisher result IDs must match input message order")
        successes = sum(
            isinstance(result, PublishSucceeded) for result in batch.results
        )
        record_dispatched(stage, successes)
        enqueued += successes
        for target, result in zip(batch_targets, batch.results, strict=True):
            failed = isinstance(result, PublishFailed)
            await append_backfill_item_event(
                session_factory,
                backfill_stage=backfill_stage,
                run_id=run_id,
                target_kind=target_kind,
                target=target.target,
                event_type=EventType.FAILED if failed else EventType.SUCCEEDED,
                outcome_code=(
                    BackfillOutcomeCode.ITEM_ENQUEUE_FAILED
                    if failed
                    else BackfillOutcomeCode.ITEM_ENQUEUED
                ),
                exc=result.error if isinstance(result, PublishFailed) else None,
            )
    logger.info(
        "backfill_completed", stage=stage, found=len(targets), requeued=enqueued
    )


async def backfill_curations(
    session_factory: SessionFactory,
    publisher: EventPublisher,
    *,
    enabled: bool,
    now: datetime,
) -> None:
    """curationの期限切れを整理し、期限内の未完了を再投入する。"""
    if not enabled:
        logger.info("backfill_curations_disabled")
        return
    async with _run(
        session_factory,
        backfill_stage="curate",
        op="backfill_curations",
    ) as run_id:
        before, after = BackfillWindow().boundaries_at(now)
        cleaned = await delete_aged_out_curations(session_factory, created_before=after)
        record_aged_out("curation", action="deleted", count=cleaned)
        async with session_factory() as session:
            backlog = PipelineBacklog(session)
            count = await backlog.count_articles_pending_curation(
                created_before=before,
                created_after=after,
            )
            targets = await backlog.curation_events_pending(
                created_before=before,
                created_after=after,
                limit=CURATIONS_LIMIT,
            )
        backlog_gauge.set(count, attributes={"stage": "curation"})
        await _dispatch(
            session_factory,
            publisher,
            targets,
            backfill_stage="curate",
            stage="curation",
            target_kind="article",
            run_id=run_id,
        )


async def backfill_assessments(
    session_factory: SessionFactory,
    publisher: EventPublisher,
    *,
    enabled: bool,
    now: datetime,
) -> None:
    """assessmentの期限切れを整理し、期限内の未完了を再投入する。"""
    if not enabled:
        logger.info("backfill_assessments_disabled")
        return
    async with _run(
        session_factory,
        backfill_stage="assess",
        op="backfill_assessments",
    ) as run_id:
        before, after = BackfillWindow().boundaries_at(now)
        cleaned = await exclude_aged_out_assessments(
            session_factory, created_before=after
        )
        record_aged_out("assessment", action="excluded", count=cleaned)
        async with session_factory() as session:
            backlog = PipelineBacklog(session)
            count = await backlog.count_curations_pending_assessment(
                created_before=before,
                created_after=after,
            )
            targets = await backlog.assessment_events_pending(
                created_before=before,
                created_after=after,
                limit=ASSESSMENTS_LIMIT,
            )
        backlog_gauge.set(count, attributes={"stage": "assessment"})
        await _dispatch(
            session_factory,
            publisher,
            targets,
            backfill_stage="assess",
            stage="assessment",
            target_kind="curation",
            run_id=run_id,
        )


async def backfill_embeddings(
    session_factory: SessionFactory,
    publisher: EventPublisher,
    *,
    enabled: bool,
    now: datetime,
) -> None:
    """embeddingの期限切れを整理し、期限内の未完了を再投入する。"""
    if not enabled:
        logger.info("backfill_embeddings_disabled")
        return
    async with _run(
        session_factory,
        backfill_stage="embed",
        op="backfill_embeddings",
    ) as run_id:
        before, after = BackfillWindow().boundaries_at(now)
        cleaned = await exclude_aged_out_embeddings(
            session_factory, created_before=after
        )
        record_aged_out("embedding", action="excluded", count=cleaned)
        async with session_factory() as session:
            backlog = PipelineBacklog(session)
            count = await backlog.count_analyzed_articles_pending_embedding(
                created_before=before,
                created_after=after,
            )
            targets = await backlog.embedding_events_pending(
                created_before=before,
                created_after=after,
                limit=EMBEDDINGS_LIMIT,
            )
        backlog_gauge.set(count, attributes={"stage": "embedding"})
        await _dispatch(
            session_factory,
            publisher,
            targets,
            backfill_stage="embed",
            stage="embedding",
            target_kind="analyzed_article",
            run_id=run_id,
        )
