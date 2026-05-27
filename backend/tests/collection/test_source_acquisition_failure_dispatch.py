"""``SourceAcquisitionFailureHandler`` の dispatch integration test。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.collection.article_acquisition.errors import (
    AcquisitionExternalFetchTerminalError,
)
from app.collection.article_acquisition.failure_handling import (
    SourceAcquisitionFailureHandler,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchSsrfBlockedError,
)
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


async def _fetch_acquisition_events(
    db_session: AsyncSession, source_id: int
) -> list[PipelineEvent]:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent)
                .where(PipelineEvent.source_id == source_id)
                .where(PipelineEvent.stage == "acquisition")
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@pytest.mark.asyncio
async def test_acquisition_error_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Stage 1 marker → origin CODE の audit 1 行 + ``reraise=False``。"""
    source_id = sample_source.id
    handler = SourceAcquisitionFailureHandler(session_factory)

    exc = AcquisitionExternalFetchTerminalError(
        origin_error=FetchAccessDeniedError(status_code=403, reason="forbidden")
    )
    reraise = await handler.handle(
        source_id=source_id,
        source_name="VentureBeat",
        exc=exc,
    )

    assert reraise is False
    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "fetch_access_denied"
    assert ev.retryability == "non_retryable"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".AcquisitionExternalFetchTerminalError")
    assert "code" not in ev.payload
    assert ev.payload["source_name"] == "VentureBeat"
    assert ev.payload["error_message"] == (
        "AcquisitionExternalFetchTerminalError(code='fetch_access_denied')"
    )
    assert ev.payload["failure_kind"] == "external_fetch"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_unexpected_error_writes_audit_and_returns_true(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """想定外 ``Exception`` → unexpected_error audit + ``reraise=True``。"""
    source_id = sample_source.id
    handler = SourceAcquisitionFailureHandler(session_factory)

    reraise = await handler.handle(
        source_id=source_id,
        source_name="VentureBeat",
        exc=RuntimeError("boom"),
    )

    assert reraise is True
    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "unexpected_error"
    assert ev.retryability == "unknown"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".RuntimeError")
    assert ev.payload["failure_kind"] == "unknown"
    assert ev.payload["failure_action"] is None


@pytest.mark.asyncio
async def test_audit_failure_falls_back_to_log_with_secrets_redacted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """audit Repository が落ちても handler は完走し redacted log に退避する。"""
    source_id = sample_source.id
    handler = SourceAcquisitionFailureHandler(session_factory)

    business_exc = AcquisitionExternalFetchTerminalError(
        origin_error=FetchSsrfBlockedError(
            "blocked Authorization: Bearer sk-live-BUSINESSSECRETabc"
        )
    )

    with (
        patch(
            "app.collection.article_acquisition.failure_handling.SourceAcquisitionAuditRepository"
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
        )

    assert reraise is False
    drops = [
        e for e in cap if e.get("event") == "source_acquisition_failure_audit_dropped"
    ]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["source_id"] == source_id
    assert drop["business_error_class"].endswith(
        ".AcquisitionExternalFetchTerminalError"
    )
    assert drop["audit_error_class"].endswith(".RuntimeError")
    assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
