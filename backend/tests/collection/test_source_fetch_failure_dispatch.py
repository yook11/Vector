"""``SourceFetchFailureHandler`` の dispatch integration test。

Stage 1 は cron 一本化 (taskiq inline retry なし、``max_retries=0``、attempt は
常に 1) のため、検証する不変条件は:

- ``SourceFetchError`` → ``pipeline_events`` 1 行 (stage=source_fetch /
  event_type=failed / ``code`` = origin CODE / ``category`` = NULL /
  ``payload`` に ``code`` キー無し) + ``reraise=False``
- 想定外 ``Exception`` → audit (``code`` = ``unexpected_error``) + ``reraise=True``
- audit Repository が落ちても handler は完走し
  ``source_fetch_failure_audit_dropped`` 構造ログにフォールバックする
  (business / audit exception の secret prefix が log field から除去される、
  red-team chain γ-2 対称化)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.collection.source_fetch.errors import SourceFetchError
from app.collection.source_fetch.failure_handling import SourceFetchFailureHandler
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


async def _fetch_source_fetch_events(
    db_session: AsyncSession, source_id: int
) -> list[PipelineEvent]:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent)
                .where(PipelineEvent.source_id == source_id)
                .where(PipelineEvent.stage == "source_fetch")
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@pytest.mark.asyncio
async def test_source_fetch_error_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """``SourceFetchError`` → origin CODE の audit 1 行 + ``reraise=False``。"""
    source_id = sample_source.id
    handler = SourceFetchFailureHandler(session_factory)

    exc = SourceFetchError("HTTP 403: VentureBeat", code="fetch_access_denied")
    reraise = await handler.handle(
        source_id=source_id,
        source_name="VentureBeat",
        exc=exc,
        attempt=1,
    )

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_source_fetch_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.code == "fetch_access_denied"
    assert ev.outcome_code == "fetch_access_denied"
    assert ev.category is None
    assert ev.attempt == 1
    assert ev.error_class is not None
    assert ev.error_class.endswith(".SourceFetchError")
    # state は top-level 軸で識別。payload に code を二重に焼かない。
    assert "code" not in ev.payload
    assert ev.payload["source_name"] == "VentureBeat"
    assert ev.payload["error_message"] == "HTTP 403: VentureBeat"


@pytest.mark.asyncio
async def test_unexpected_error_writes_audit_and_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """想定外 ``Exception`` → ``code='unexpected_error'`` audit + ``reraise=True``。"""
    source_id = sample_source.id
    handler = SourceFetchFailureHandler(session_factory)

    reraise = await handler.handle(
        source_id=source_id,
        source_name="VentureBeat",
        exc=RuntimeError("boom"),
        attempt=1,
    )

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_source_fetch_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.code == "unexpected_error"
    assert ev.category is None
    assert ev.error_class is not None
    assert ev.error_class.endswith(".RuntimeError")


@pytest.mark.asyncio
async def test_audit_failure_falls_back_to_log_with_secrets_redacted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """audit Repository が落ちても handler は完走しログにフォールバックする。

    business / audit exception message に混入した secret prefix が log field
    から redact されることも検証する (red-team chain γ-2 対称化)。
    """
    source_id = sample_source.id
    handler = SourceFetchFailureHandler(session_factory)

    business_exc = SourceFetchError(
        "blocked Authorization: Bearer sk-live-BUSINESSSECRETabc",
        code="fetch_ssrf_blocked",
    )

    with (
        patch(
            "app.collection.source_fetch.failure_handling.SourceFetchAuditRepository"
        ) as mock_audit_cls,
        capture_logs() as cap,
    ):
        mock_audit_cls.return_value.append_failure = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )
        reraise = await handler.handle(
            source_id=source_id,
            source_name="VentureBeat",
            exc=business_exc,
            attempt=1,
        )

    assert reraise is False
    drops = [e for e in cap if e.get("event") == "source_fetch_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["source_id"] == source_id
    assert drop["attempt"] == 1
    assert drop["business_error_class"].endswith(".SourceFetchError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
