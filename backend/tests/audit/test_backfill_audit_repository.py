"""``BackfillAuditRepository`` の永続化 contract tests。"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.stages.backfill import BackfillAuditRepository, BackfillOutcomeCode
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


@pytest.fixture
async def article_row(db_session: AsyncSession, sample_source: NewsSource) -> Article:
    """backfill item audit 用 article を作成する。"""
    article = Article(
        source_id=sample_source.id,
        source_url="https://example.com/backfill-audit",  # type: ignore[arg-type]
        original_title="title",
        original_content="content" * 20,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


@pytest.mark.asyncio
async def test_append_item_event_records_target_snapshot(
    db_session: AsyncSession,
    article_row: Article,
    sample_source: NewsSource,
) -> None:
    """item enqueue 成功は target と article/source を保存する。"""
    repo = BackfillAuditRepository(db_session)

    await repo.append_item_event(
        stage=Stage.BACKFILL_ASSESS,
        event_type=EventType.SUCCEEDED,
        outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
        backfill_stage="assess",
        run_id="run-1",
        target_kind="curation",
        target_id=123,
        article_id=article_row.id,
        source_name=str(sample_source.name),
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.stage == "backfill_assess"
    assert row.event_type == "succeeded"
    assert row.outcome_code == "backfill_item_enqueued"
    assert row.article_id == article_row.id
    assert row.source_id == sample_source.id
    assert row.payload["kind"] == "backfill"
    assert row.payload["backfill_stage"] == "assess"
    assert row.payload["run_id"] == "run-1"
    assert row.payload["target_kind"] == "curation"
    assert row.payload["target_id"] == 123
    assert row.payload["source_name"] == str(sample_source.name)


@pytest.mark.asyncio
async def test_append_run_event_records_counts_and_error(
    db_session: AsyncSession,
) -> None:
    """run failed は count snapshot と例外情報を保存する。"""
    repo = BackfillAuditRepository(db_session)
    exc = RuntimeError("select failed")

    await repo.append_run_event(
        stage=Stage.BACKFILL_EMBED,
        event_type=EventType.FAILED,
        outcome_code=BackfillOutcomeCode.RUN_FAILED,
        backfill_stage="embed",
        run_id="run-2",
        selected_count=50,
        granted_count=10,
        enqueued_count=9,
        failed_count=1,
        limit=50,
        daily_max=1500,
        exc=exc,
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.stage == "backfill_embed"
    assert row.event_type == "failed"
    assert row.outcome_code == "backfill_run_failed"
    assert row.error_class == "builtins.RuntimeError"
    assert row.payload["kind"] == "backfill"
    assert row.payload["backfill_stage"] == "embed"
    assert row.payload["run_id"] == "run-2"
    assert row.payload["selected_count"] == 50
    assert row.payload["granted_count"] == 10
    assert row.payload["enqueued_count"] == 9
    assert row.payload["failed_count"] == 1
    assert row.payload["limit"] == 50
    assert row.payload["daily_max"] == 1500
    assert row.payload["error_message"] == "select failed"
    assert row.payload["error_chain"] == ["builtins.RuntimeError"]
