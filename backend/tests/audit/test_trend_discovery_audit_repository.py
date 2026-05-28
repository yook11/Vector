"""``TrendDiscoveryAuditRepository`` の永続化 contract tests。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType
from app.audit.stages.trend_discovery import (
    TrendDiscoveryAuditRepository,
    TrendDiscoveryOutcomeCode,
)
from app.models.pipeline_event import PipelineEvent


@pytest.mark.asyncio
async def test_append_run_event_records_completed_payload(
    db_session: AsyncSession,
) -> None:
    """run completed は window / trigger / count snapshot を保存する。"""
    repo = TrendDiscoveryAuditRepository(db_session)

    await repo.append_run_event(
        event_type=EventType.SUCCEEDED,
        outcome_code=TrendDiscoveryOutcomeCode.RUN_COMPLETED,
        window_start=date(2026, 4, 26),
        window_end=date(2026, 5, 3),
        trigger="cron",
        requested_update=False,
        source_analysis_count=42,
        completed_category_count=3,
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.stage == "trend_discovery"
    assert row.event_type == "succeeded"
    assert row.outcome_code == "trend_discovery_run_completed"
    assert row.retryability is None
    assert row.payload["kind"] == "trend_discovery"
    assert row.payload["window_start"] == "2026-04-26"
    assert row.payload["window_end"] == "2026-05-03"
    assert row.payload["trigger"] == "cron"
    assert row.payload["requested_update"] is False
    assert row.payload["source_analysis_count"] == 42
    assert row.payload["completed_category_count"] == 3


@pytest.mark.asyncio
async def test_append_run_event_records_failure_error_fields(
    db_session: AsyncSession,
) -> None:
    """run failed は例外情報と retryability=unknown を保存する。"""
    repo = TrendDiscoveryAuditRepository(db_session)
    exc = RuntimeError("select failed")

    await repo.append_run_event(
        event_type=EventType.FAILED,
        outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
        window_start=date(2026, 4, 26),
        window_end=date(2026, 5, 3),
        trigger="cli",
        requested_update=True,
        exc=exc,
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.stage == "trend_discovery"
    assert row.event_type == "failed"
    assert row.outcome_code == "trend_discovery_run_failed"
    assert row.error_class == "builtins.RuntimeError"
    assert row.retryability == "unknown"
    assert row.payload["trigger"] == "cli"
    assert row.payload["requested_update"] is True
    assert row.payload["source_analysis_count"] is None
    assert row.payload["completed_category_count"] is None
    assert row.payload["error_message"] == "select failed"
    assert row.payload["error_chain"] == ["builtins.RuntimeError"]
