"""実DBを使って工程別の救済対象と再投入の振る舞いを確認する。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.audit.domain.event import Stage
from app.audit.stages.backfill import BackfillAuditRepository
from app.backfill import service
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    EmbeddingBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord
from app.models.outbox_event import OutboxEvent
from app.models.pipeline_event import PipelineEvent
from tests.backfill.helpers import RecordingPublisher, complete_target, seed_target

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
CASES = [
    ("curation", service.backfill_curations),
    ("assessment", service.backfill_assessments),
    ("embedding", service.backfill_embeddings),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "entry"), CASES)
async def test_window_and_saved_event_facts(
    db_session, session_factory, sample_source, sample_categories, stage, entry
):
    """30分と7日の境界を元記事年齢で判定し、保存済み事実を再投入する。"""
    offsets = [
        timedelta(minutes=30) - timedelta(microseconds=1),
        timedelta(minutes=30),
        timedelta(minutes=30, microseconds=1),
        timedelta(days=7),
        timedelta(days=7, microseconds=1),
    ]
    records = [
        await seed_target(
            db_session, sample_source, sample_categories[0], stage, NOW - age
        )
        for age in offsets
    ]
    publisher = RecordingPublisher()
    await entry(session_factory, publisher, enabled=True, now=NOW)
    expected = [records[3], records[2]]
    key = {
        "curation": "analyzable_article_id",
        "assessment": "curation_id",
        "embedding": "analyzed_article_id",
    }[stage]
    assert [item.payload[key] for item in publisher.envelopes] == [
        item.target_id for item in expected
    ]
    assert [item.occurred_at for item in publisher.envelopes] == [
        item.occurred_at for item in expected
    ]
    if stage == "assessment":
        assert [
            item.payload["analyzable_article_id"] for item in publisher.envelopes
        ] == [item.article_id for item in expected]
    if stage == "embedding":
        assert [item.payload["curation_id"] for item in publisher.envelopes] == [
            item.curation_id for item in expected
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "entry"), CASES)
async def test_completed_and_excluded_targets_are_not_sent(
    db_session, session_factory, sample_source, sample_categories, stage, entry
):
    """正常な終了結果と救済除外を再投入しない。"""
    done = await seed_target(
        db_session, sample_source, sample_categories[0], stage, NOW - timedelta(days=1)
    )
    await complete_target(db_session, done, stage, sample_categories[0])
    await db_session.commit()
    other = await seed_target(
        db_session, sample_source, sample_categories[0], stage, NOW - timedelta(days=1)
    )
    if stage == "curation":
        db_session.add(
            CurationNoise(
                analyzable_article_id=other.article_id,
                translated_title="noise",
                summary="noise",
            )
        )
    elif stage == "assessment":
        db_session.add(
            OutOfScopeArticleRecord(
                curation_id=other.target_id,
                translated_title="title",
                summary="summary",
                investor_take="take",
            )
        )
    else:
        db_session.add(
            EmbeddingBackfillExclusion(
                analyzed_article_id=other.target_id,
                reason_code="backfill_embedding_aged_out",
            )
        )
    if stage == "assessment":
        excluded = await seed_target(
            db_session,
            sample_source,
            sample_categories[0],
            stage,
            NOW - timedelta(days=1),
        )
        db_session.add(
            AssessmentBackfillExclusion(
                curation_id=excluded.target_id,
                reason_code="backfill_assessment_aged_out",
            )
        )
    await db_session.commit()
    publisher = RecordingPublisher()
    await entry(session_factory, publisher, enabled=True, now=NOW)
    assert publisher.envelopes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "entry"), CASES)
async def test_run_limit_keeps_oldest_fifty(
    db_session, session_factory, sample_source, sample_categories, stage, entry
):
    """一回の送信を50件に制限し、古い記事を優先する。"""
    records = [
        await seed_target(
            db_session,
            sample_source,
            sample_categories[0],
            stage,
            NOW - timedelta(hours=1, minutes=i),
        )
        for i in range(51)
    ]
    publisher = RecordingPublisher()
    await entry(session_factory, publisher, enabled=True, now=NOW)
    key = {
        "curation": "analyzable_article_id",
        "assessment": "curation_id",
        "embedding": "analyzed_article_id",
    }[stage]
    assert [item.payload[key] for item in publisher.envelopes] == [
        item.target_id for item in reversed(records[1:])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "entry"), CASES)
async def test_unfinished_target_is_resent_without_redis_or_outbox(
    db_session,
    session_factory,
    sample_source,
    sample_categories,
    monkeypatch,
    stage,
    entry,
):
    """RedisやOutboxに依存せず、未完了の記事は次回も送る。"""
    from app.queue.helpers import budget, stage_hold

    budget_call = AsyncMock(side_effect=AssertionError("budget must not be used"))
    hold_call = AsyncMock(side_effect=AssertionError("hold must not be used"))
    monkeypatch.setattr(budget, "consume_daily_budget", budget_call)
    monkeypatch.setattr(stage_hold, "is_stage_held", hold_call)
    monkeypatch.setattr(stage_hold, "set_stage_hold", hold_call)
    await seed_target(
        db_session, sample_source, sample_categories[0], stage, NOW - timedelta(days=1)
    )
    publisher = RecordingPublisher()
    await entry(session_factory, publisher, enabled=True, now=NOW)
    await entry(
        session_factory, publisher, enabled=True, now=NOW + timedelta(minutes=30)
    )
    assert len(publisher.envelopes) == 2
    assert publisher.envelopes[0].event_id != publisher.envelopes[1].event_id
    assert publisher.envelopes[0].payload == publisher.envelopes[1].payload
    assert await db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
    budget_call.assert_not_called()
    hold_call.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_commits_before_publishing(
    db_session, session_factory, sample_source, sample_categories
):
    """送信開始時には期限切れ整理が他の接続からも確定済みである。"""
    old = await seed_target(
        db_session,
        sample_source,
        sample_categories[0],
        "curation",
        NOW - timedelta(days=8),
    )
    await seed_target(
        db_session,
        sample_source,
        sample_categories[0],
        "curation",
        NOW - timedelta(days=1),
    )

    class FailingPublisher:
        def publish_batch(self, envelopes):
            raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        await service.backfill_curations(
            session_factory, FailingPublisher(), enabled=True, now=NOW
        )
    assert await db_session.get(AnalyzableArticleRecord, old.article_id) is None
    failed = await db_session.scalar(
        select(PipelineEvent).where(PipelineEvent.outcome_code == "backfill_run_failed")
    )
    assert failed is not None


@pytest.mark.asyncio
async def test_audit_failure_does_not_stop_sending(
    db_session, session_factory, sample_source, sample_categories, monkeypatch
):
    """受付後の監査・診断が失敗しても次の対象へ進む。"""
    from app.backfill import audit

    for i in range(11):
        await seed_target(
            db_session,
            sample_source,
            sample_categories[0],
            "curation",
            NOW - timedelta(hours=1, minutes=i),
        )
    monkeypatch.setattr(
        BackfillAuditRepository,
        "append_item_event",
        AsyncMock(side_effect=RuntimeError("audit failed")),
    )
    monkeypatch.setattr(
        audit,
        "record_audit_dropped",
        lambda *args: (_ for _ in ()).throw(RuntimeError("metric failed")),
    )
    publisher = RecordingPublisher()
    await service.backfill_curations(session_factory, publisher, enabled=True, now=NOW)
    assert len(publisher.envelopes) == 11
    assert await db_session.scalar(select(func.count()).select_from(PipelineEvent)) == 0


@pytest.mark.asyncio
async def test_database_failure_is_recorded_and_propagated(session_factory):
    """実DBの接続障害を成功扱いせず、実行失敗を記録する。"""
    from contextlib import asynccontextmanager

    calls = 0

    @asynccontextmanager
    async def unavailable_first():
        nonlocal calls
        calls += 1
        async with session_factory() as session:
            if calls == 1:
                await session.close()
                from sqlalchemy import text

                await session.execute(text("SELECT missing_backfill_column"))
            yield session

    with pytest.raises(Exception, match="missing_backfill_column"):
        await service.backfill_curations(
            unavailable_first, RecordingPublisher(), enabled=True, now=NOW
        )
    async with session_factory() as session:
        failure = await session.scalar(
            select(PipelineEvent).where(
                PipelineEvent.stage == Stage.BACKFILL_CURATE.value
            )
        )
        assert failure.outcome_code == "backfill_run_failed"
