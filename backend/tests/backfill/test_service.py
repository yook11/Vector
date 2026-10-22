"""実DBを使って工程別の救済対象と再投入の振る舞いを確認する。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.backfill import service
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.backfill_exclusion import (
    AssessmentBackfillExclusion,
    EmbeddingBackfillExclusion,
)
from app.models.curation_noise import CurationNoise
from app.models.incomplete_article import IncompleteArticle
from app.models.out_of_scope_article_record import OutOfScopeArticleRecord
from app.models.outbox_event import OutboxEvent
from tests.backfill.helpers import (
    RecordingSender,
    complete_target,
    delivery_arguments,
    seed_target,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
CASES = [
    ("curation", service.backfill_curations),
    ("assessment", service.backfill_assessments),
    ("embedding", service.backfill_embeddings),
    ("completion", service.backfill_completions),
]
PAYLOAD_KEY = {
    "curation": "analyzable_article_id",
    "assessment": "curation_id",
    "embedding": "analyzed_article_id",
    "completion": "incomplete_article_id",
}


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
    sender = RecordingSender()
    await entry(
        session_factory, sender, **delivery_arguments(stage), enabled=True, now=NOW
    )
    expected = [records[3], records[2]]
    key = PAYLOAD_KEY[stage]
    assert [item.payload[key] for item in sender.envelopes] == [
        item.target_id for item in expected
    ]
    assert [item.occurred_at for item in sender.envelopes] == [
        item.occurred_at for item in expected
    ]
    if stage == "assessment":
        assert [item.payload["analyzable_article_id"] for item in sender.envelopes] == [
            item.article_id for item in expected
        ]
    if stage == "embedding":
        assert [item.payload["curation_id"] for item in sender.envelopes] == [
            item.curation_id for item in expected
        ]
    if stage == "completion":
        assert [item.payload["source_id"] for item in sender.envelopes] == [
            sample_source.id for _ in expected
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
    elif stage == "embedding":
        db_session.add(
            EmbeddingBackfillExclusion(
                analyzed_article_id=other.target_id,
                reason_code="backfill_embedding_aged_out",
            )
        )
    else:
        # 補完成功は未完成行を削除するため、消えた行は再投入の対象にならない。
        await db_session.delete(
            await db_session.get(IncompleteArticle, other.target_id)
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
    sender = RecordingSender()
    await entry(
        session_factory, sender, **delivery_arguments(stage), enabled=True, now=NOW
    )
    assert sender.envelopes == []


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
    sender = RecordingSender()
    await entry(
        session_factory, sender, **delivery_arguments(stage), enabled=True, now=NOW
    )
    assert [item.payload[PAYLOAD_KEY[stage]] for item in sender.envelopes] == [
        item.target_id for item in reversed(records[1:])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "entry"), CASES)
async def test_backfill_does_not_use_outbox(
    db_session, session_factory, sample_source, sample_categories, stage, entry
):
    """再投入は送信先へ直接渡し、Outboxへ保存しない。"""
    await seed_target(
        db_session, sample_source, sample_categories[0], stage, NOW - timedelta(days=1)
    )
    sender = RecordingSender()
    await entry(
        session_factory, sender, **delivery_arguments(stage), enabled=True, now=NOW
    )
    assert len(sender.envelopes) == 1
    assert await db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.asyncio
async def test_cleanup_commits_before_sending(
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

    class FailingSender:
        def send_batch(self, *, queue_url, send_requests):
            raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        await service.backfill_curations(
            session_factory,
            FailingSender(),
            **delivery_arguments("curation"),
            enabled=True,
            now=NOW,
        )
    assert await db_session.get(AnalyzableArticleRecord, old.article_id) is None


@pytest.mark.asyncio
async def test_database_failure_is_propagated(session_factory):
    """実DBの接続障害を成功扱いせず、呼び出し元へ伝える。"""
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
            unavailable_first,
            RecordingSender(),
            **delivery_arguments("curation"),
            enabled=True,
            now=NOW,
        )
