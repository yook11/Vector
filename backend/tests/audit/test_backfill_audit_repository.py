"""``BackfillAuditRepository`` の永続化 contract tests。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import BackfillPayload
from app.audit.stages.backfill import BackfillAuditRepository, BackfillOutcomeCode
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


@pytest.fixture
async def article_row(
    db_session: AsyncSession, sample_source: NewsSource
) -> AnalyzableArticleRecord:
    """backfill item audit 用 article を作成する。"""
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/backfill-audit",  # type: ignore[arg-type]
        original_title="title",
        original_content="content" * 20,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


@pytest.mark.parametrize(
    "method_name",
    ["append_item_event", "append_run_event"],
)
def test_public_backfill_audit_api_derives_stage_internally(
    method_name: str,
) -> None:
    """caller は stage を渡せず、backfill_stage だけが public な stage 入力になる。"""
    params = inspect.signature(getattr(BackfillAuditRepository, method_name)).parameters

    assert "stage" not in params
    assert "backfill_stage" in params


@pytest.mark.parametrize(
    ("backfill_stage", "expected_stage"),
    [
        ("curate", Stage.BACKFILL_CURATE),
        ("assess", Stage.BACKFILL_ASSESS),
        ("embed", Stage.BACKFILL_EMBED),
    ],
)
def test_stage_for_maps_backfill_stage_to_event_stage(
    backfill_stage: str, expected_stage: Stage
) -> None:
    assert BackfillAuditRepository.stage_for(backfill_stage) is expected_stage  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backfill_stage", "expected_stage"),
    [
        ("curate", "backfill_curate"),
        ("assess", "backfill_assess"),
        ("embed", "backfill_embed"),
    ],
)
async def test_append_run_event_derives_pipeline_stage_from_backfill_stage(
    db_session: AsyncSession,
    backfill_stage: str,
    expected_stage: str,
) -> None:
    repo = BackfillAuditRepository(db_session)

    await repo.append_run_event(
        event_type=EventType.FAILED,
        outcome_code=BackfillOutcomeCode.RUN_FAILED,
        backfill_stage=backfill_stage,  # type: ignore[arg-type]
        run_id=f"run-{backfill_stage}",
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.stage == expected_stage
    assert row.payload["backfill_stage"] == backfill_stage


@pytest.mark.asyncio
async def test_append_item_event_records_target_snapshot(
    db_session: AsyncSession,
    article_row: AnalyzableArticleRecord,
    sample_source: NewsSource,
) -> None:
    """item enqueue 成功は target と article/source を保存する。"""
    repo = BackfillAuditRepository(db_session)

    await repo.append_item_event(
        event_type=EventType.SUCCEEDED,
        outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
        backfill_stage="assess",
        run_id="run-1",
        target_kind="curation",
        target_id=123,
        analyzable_article_id=article_row.id,
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
async def test_append_embed_item_event_records_analyzed_article_target_kind(
    db_session: AsyncSession,
    article_row: AnalyzableArticleRecord,
    sample_source: NewsSource,
) -> None:
    repo = BackfillAuditRepository(db_session)

    await repo.append_item_event(
        event_type=EventType.SUCCEEDED,
        outcome_code=BackfillOutcomeCode.ITEM_ENQUEUED,
        backfill_stage="embed",
        run_id="run-embed",
        target_kind="analyzed_article",
        target_id=456,
        analyzable_article_id=article_row.id,
        source_name=str(sample_source.name),
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.stage == "backfill_embed"
    assert row.payload["target_kind"] == "analyzed_article"
    assert row.payload["target_id"] == 456


def test_backfill_payload_rejects_legacy_analysis_target_kind() -> None:
    with pytest.raises(ValidationError):
        BackfillPayload(
            backfill_stage="embed",
            target_kind="analysis",
            target_id=456,
        )


@pytest.mark.asyncio
async def test_append_run_failed_records_error_without_counts(
    db_session: AsyncSession,
) -> None:
    """run failed は例外情報を保存し、throughput count は焼かない (保証2)。"""
    repo = BackfillAuditRepository(db_session)
    exc = RuntimeError("select failed")

    await repo.append_run_event(
        event_type=EventType.FAILED,
        outcome_code=BackfillOutcomeCode.RUN_FAILED,
        backfill_stage="embed",
        run_id="run-2",
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
    assert row.payload["error_message"] == "select failed"
    assert row.payload["error_chain"] == ["builtins.RuntimeError"]
    for removed in (
        "selected_count",
        "granted_count",
        "enqueued_count",
        "failed_count",
        "limit",
    ):
        assert removed not in row.payload


@pytest.mark.asyncio
async def test_append_run_budget_exhausted_records_daily_max(
    db_session: AsyncSession,
) -> None:
    """daily budget 枯渇は停止閾値 daily_max のみ保存する (保証2)。"""
    repo = BackfillAuditRepository(db_session)

    await repo.append_run_event(
        event_type=EventType.SKIPPED,
        outcome_code=BackfillOutcomeCode.RUN_DAILY_BUDGET_EXHAUSTED,
        backfill_stage="embed",
        run_id="run-3",
        daily_max=1500,
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.outcome_code == "backfill_run_daily_budget_exhausted"
    assert row.payload["daily_max"] == 1500
    assert "limit" not in row.payload
