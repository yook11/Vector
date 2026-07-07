"""``app.queue.tasks.retention`` の retention purge テスト。

- 90 日経過行が削除され、新しい行は残ること
- kill switch (`pipeline_events_retention_enabled=False`) で skip すること
- max_batches 上限で 1 回の実行に上限が効くこと
- 削除対象が空なら早期離脱して log だけ出すこと
- `auth."rateLimit"` は 10 分より古い行だけ batch 削除すること
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


def _auth_ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """ctx.state.auth_session_factory を持つ Context モックを返す。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(auth_session_factory=session_factory)
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


async def _reset_auth_rate_limit_table(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS auth."rateLimit" (
                "key" text PRIMARY KEY,
                "count" integer NOT NULL,
                "lastRequest" bigint NOT NULL
            )
            """
        )
    )
    await db_session.execute(text('TRUNCATE TABLE auth."rateLimit"'))
    await db_session.commit()


async def _insert_auth_rate_limit(
    db_session: AsyncSession, *, key: str, last_request_ms: int
) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO auth."rateLimit" ("key", "count", "lastRequest")
            VALUES (:key, 1, :last_request_ms)
            """
        ),
        {"key": key, "last_request_ms": last_request_ms},
    )
    await db_session.commit()


async def _auth_rate_limit_keys(db_session: AsyncSession) -> set[str]:
    rows = await db_session.execute(text('SELECT "key" FROM auth."rateLimit"'))
    return set(rows.scalars().all())


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


@pytest.mark.asyncio
async def test_purge_auth_rate_limits_deletes_older_than_ten_minutes_only(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """11 分前 row は削除、9 分前 row は残る。"""
    await _reset_auth_rate_limit_table(db_session)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    old_key = "203.0.113.10|/sign-in/email"
    recent_key = "203.0.113.20|/sign-in/email"
    await _insert_auth_rate_limit(
        db_session, key=old_key, last_request_ms=now_ms - 11 * 60 * 1000
    )
    await _insert_auth_rate_limit(
        db_session, key=recent_key, last_request_ms=now_ms - 9 * 60 * 1000
    )

    with capture_logs() as logs:
        await retention.purge_auth_rate_limits(ctx=_auth_ctx(session_factory))

    remaining = await _auth_rate_limit_keys(db_session)
    assert old_key not in remaining
    assert recent_key in remaining
    purged_log = next(
        log for log in logs if log["event"] == "auth_rate_limit_retention_purged"
    )
    assert purged_log["retention_seconds"] == 600
    assert "203.0.113" not in str(logs)


@pytest.mark.asyncio
async def test_purge_auth_rate_limits_kill_switch_skips_execution(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    await _reset_auth_rate_limit_table(db_session)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await _insert_auth_rate_limit(
        db_session,
        key="203.0.113.30|/sign-in/email",
        last_request_ms=now_ms - 11 * 60 * 1000,
    )

    with (
        patch.object(retention.settings, "auth_rate_limit_retention_enabled", False),
        capture_logs() as logs,
    ):
        await retention.purge_auth_rate_limits(ctx=_auth_ctx(session_factory))

    assert await _auth_rate_limit_keys(db_session) == {"203.0.113.30|/sign-in/email"}
    assert any(log["event"] == "auth_rate_limit_retention_disabled" for log in logs)


@pytest.mark.asyncio
async def test_purge_auth_rate_limits_empty_target_logs_zero_deleted(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    await _reset_auth_rate_limit_table(db_session)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await _insert_auth_rate_limit(
        db_session,
        key="203.0.113.40|/sign-in/email",
        last_request_ms=now_ms - 9 * 60 * 1000,
    )

    with capture_logs() as logs:
        await retention.purge_auth_rate_limits(ctx=_auth_ctx(session_factory))

    purged_log = next(
        log for log in logs if log["event"] == "auth_rate_limit_retention_purged"
    )
    assert purged_log["deleted"] == 0
    assert purged_log["batches"] == 0


@pytest.mark.asyncio
async def test_purge_auth_rate_limits_respects_max_batches_cap(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    await _reset_auth_rate_limit_table(db_session)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    for i in range(3):
        await _insert_auth_rate_limit(
            db_session,
            key=f"203.0.113.{50 + i}|/sign-in/email",
            last_request_ms=now_ms - (11 + i) * 60 * 1000,
        )

    with (
        patch.object(retention.settings, "auth_rate_limit_retention_max_batches", 1),
        patch.object(retention, "AUTH_RATE_LIMIT_BATCH_SIZE", 1),
        capture_logs() as logs,
    ):
        await retention.purge_auth_rate_limits(ctx=_auth_ctx(session_factory))

    assert len(await _auth_rate_limit_keys(db_session)) == 2
    purged_log = next(
        log for log in logs if log["event"] == "auth_rate_limit_retention_purged"
    )
    assert purged_log["deleted"] == 1
    assert purged_log["batches"] == 1


@pytest.mark.asyncio
async def test_purge_auth_rate_limits_missing_session_factory_logs_failed() -> None:
    ctx = MagicMock()
    ctx.state = SimpleNamespace()

    with capture_logs() as logs:
        await retention.purge_auth_rate_limits(ctx=ctx)

    failed_log = next(
        log for log in logs if log["event"] == "auth_rate_limit_retention_failed"
    )
    assert failed_log["error_type"] == "RuntimeError"
    assert failed_log["reason"] == "auth_session_factory_missing"


@pytest.mark.asyncio
async def test_purge_auth_rate_limits_sql_failure_does_not_log_secret_or_key() -> None:
    class FailingSession:
        async def __aenter__(self) -> FailingSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                "postgresql://vector_auth:super-secret@db/vector 203.0.113.99"
            )

    ctx = _auth_ctx(lambda: FailingSession())  # type: ignore[arg-type]

    with capture_logs() as logs:
        await retention.purge_auth_rate_limits(ctx=ctx)

    failed_log = next(
        log for log in logs if log["event"] == "auth_rate_limit_retention_failed"
    )
    assert failed_log["error_type"] == "RuntimeError"
    assert "super-secret" not in str(logs)
    assert "203.0.113.99" not in str(logs)
