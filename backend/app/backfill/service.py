"""工程別に期限切れを整理し、未完了記事のイベントを再投入する。"""

from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

import structlog

from app.backfill.cleanup import (
    close_aged_out_completions,
    delete_aged_out_curations,
    exclude_aged_out_assessments,
    exclude_aged_out_embeddings,
)
from app.backfill.metrics import backlog_gauge, record_aged_out, record_dispatched
from app.backfill.policy import (
    ASSESSMENTS_LIMIT,
    COMPLETIONS_LIMIT,
    CURATIONS_LIMIT,
    EMBEDDINGS_LIMIT,
    BackfillWindow,
)
from app.backfill.repository import PipelineBacklog
from app.backfill.targets import BackfillEventTarget
from app.db.session import SessionFactory
from app.outbox.publishing.event_batch import MAX_BATCH_MESSAGES
from app.outbox.publishing.publisher import (
    BatchPublishResult,
    EventEnvelope,
    EventPublisher,
    PublishSucceeded,
)

logger = structlog.get_logger(__name__)


async def _dispatch(
    publisher: EventPublisher,
    targets: Sequence[BackfillEventTarget],
    *,
    stage: str,
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
    await _dispatch(publisher, targets, stage="curation")


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
    before, after = BackfillWindow().boundaries_at(now)
    cleaned = await exclude_aged_out_assessments(session_factory, created_before=after)
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
    await _dispatch(publisher, targets, stage="assessment")


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
    before, after = BackfillWindow().boundaries_at(now)
    cleaned = await exclude_aged_out_embeddings(session_factory, created_before=after)
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
    await _dispatch(publisher, targets, stage="embedding")


async def backfill_completions(
    session_factory: SessionFactory,
    publisher: EventPublisher,
    *,
    enabled: bool,
    now: datetime,
) -> None:
    """補完の期限切れをclosedにし、期限内の未完成行を再投入する。"""
    if not enabled:
        logger.info("backfill_completions_disabled")
        return
    before, after = BackfillWindow().boundaries_at(now)
    cleaned = await close_aged_out_completions(session_factory, created_before=after)
    record_aged_out("completion", action="closed", count=cleaned)
    async with session_factory() as session:
        backlog = PipelineBacklog(session)
        count = await backlog.count_incomplete_articles_pending_completion(
            created_before=before,
            created_after=after,
        )
        targets = await backlog.completion_events_pending(
            created_before=before,
            created_after=after,
            limit=COMPLETIONS_LIMIT,
        )
    backlog_gauge.set(count, attributes={"stage": "completion"})
    await _dispatch(publisher, targets, stage="completion")
