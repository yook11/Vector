"""``app.queue.tasks.retention.purge_pipeline_events`` のテスト。

- 90 日経過行が削除され、新しい行は残ること
- kill switch (`pipeline_events_retention_enabled=False`) で skip すること
- max_batches 上限で 1 回の実行に上限が効くこと
- 削除対象が空なら早期離脱して log だけ出すこと
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.models.pipeline_event import PipelineEvent
from app.queue.tasks import retention


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """ctx.state.session_factory を持つ Context モックを返す。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=session_factory)
    return ctx


async def _insert_event_at(
    db_session: AsyncSession, *, occurred_at: datetime, outcome_code: str
) -> None:
    """指定 ``occurred_at`` で 1 行 insert する。"""
    await db_session.execute(
        text(
            """
            INSERT INTO pipeline_events
                (occurred_at, stage, event_type, outcome_code, payload)
            VALUES (:occurred_at, 'acquisition', 'failed', :code, '{}'::jsonb)
            """
        ),
        {"occurred_at": occurred_at, "code": outcome_code},
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_purge_deletes_old_rows_and_preserves_recent(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """100 日前 row は削除、10 日前 row は残る。"""
    now = datetime.now(UTC)
    await _insert_event_at(
        db_session, occurred_at=now - timedelta(days=100), outcome_code="old_evt"
    )
    await _insert_event_at(
        db_session, occurred_at=now - timedelta(days=10), outcome_code="recent_evt"
    )

    await retention.purge_pipeline_events(ctx=_ctx(session_factory))

    remaining = (
        (await db_session.execute(select(PipelineEvent.outcome_code))).scalars().all()
    )
    assert "old_evt" not in remaining
    assert "recent_evt" in remaining


@pytest.mark.asyncio
async def test_purge_kill_switch_skips_execution(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    await _insert_event_at(
        db_session, occurred_at=now - timedelta(days=100), outcome_code="old_evt"
    )

    with (
        patch.object(retention.settings, "pipeline_events_retention_enabled", False),
        capture_logs() as logs,
    ):
        await retention.purge_pipeline_events(ctx=_ctx(session_factory))

    # 古い行は残っている (delete されていない)
    count = await db_session.execute(select(func.count(PipelineEvent.id)))
    assert count.scalar_one() == 1
    assert any(log["event"] == "pipeline_events_retention_disabled" for log in logs)


@pytest.mark.asyncio
async def test_purge_empty_target_logs_zero_deleted(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """削除対象が空なら deleted=0 / batches=0 で早期離脱する。"""
    now = datetime.now(UTC)
    await _insert_event_at(
        db_session, occurred_at=now - timedelta(days=10), outcome_code="recent_evt"
    )

    with capture_logs() as logs:
        await retention.purge_pipeline_events(ctx=_ctx(session_factory))

    purged_log = next(
        log for log in logs if log["event"] == "pipeline_events_retention_purged"
    )
    assert purged_log["deleted"] == 0
    assert purged_log["batches"] == 0


@pytest.mark.asyncio
async def test_purge_respects_max_batches_cap(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """max_batches=1 + BATCH_SIZE=1 で 1 行のみ削除、残りは次回。"""
    now = datetime.now(UTC)
    for i in range(3):
        await _insert_event_at(
            db_session,
            occurred_at=now - timedelta(days=100 + i),
            outcome_code=f"old_{i}",
        )

    with (
        patch.object(retention.settings, "pipeline_events_retention_max_batches", 1),
        patch.object(retention, "BATCH_SIZE", 1),
        capture_logs() as logs,
    ):
        await retention.purge_pipeline_events(ctx=_ctx(session_factory))

    remaining = await db_session.execute(select(func.count(PipelineEvent.id)))
    # 3 行のうち 1 行のみ削除 → 残り 2
    assert remaining.scalar_one() == 2
    purged_log = next(
        log for log in logs if log["event"] == "pipeline_events_retention_purged"
    )
    assert purged_log["deleted"] == 1
    assert purged_log["batches"] == 1
