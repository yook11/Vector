"""``ArticleAcquisitionFailureHandler`` の dispatch integration test。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.collection.article_acquisition.errors import (
    AcquisitionExternalFetchError,
)
from app.collection.article_acquisition.failure_handling import (
    ArticleAcquisitionFailureHandler,
)
from app.collection.article_acquisition.fetched_article_converter import (
    ConversionRejection,
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


def _conversion_rejection() -> ConversionRejection:
    """title 欠落の棄却値 (acquisition 所有の reason、cause 無し)。"""
    return ConversionRejection(
        outcome_code="acquisition_conversion_title_missing",
        source_name="VentureBeat",
        raw_url="https://venturebeat.com/rejected",
        has_title=True,
        body_length=42,
        has_published_at=False,
        cause=None,
    )


@pytest.mark.asyncio
async def test_acquisition_error_writes_audit_and_returns_false(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Stage 1 marker → origin CODE の audit 1 行 + ``reraise=False``。"""
    source_id = sample_source.id
    handler = ArticleAcquisitionFailureHandler(session_factory)

    exc = AcquisitionExternalFetchError(
        origin_error=FetchAccessDeniedError(status_code=403, reason="forbidden")
    )
    reraise = await handler.handle_source_failure(
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
    assert ev.error_class.endswith(".AcquisitionExternalFetchError")
    assert "code" not in ev.payload
    assert ev.payload["source_name"] == "VentureBeat"
    assert ev.payload["error_message"] == (
        "AcquisitionExternalFetchError(code='fetch_access_denied')"
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
    handler = ArticleAcquisitionFailureHandler(session_factory)

    reraise = await handler.handle_source_failure(
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
    handler = ArticleAcquisitionFailureHandler(session_factory)

    business_exc = AcquisitionExternalFetchError(
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
        reraise = await handler.handle_source_failure(
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
    assert drop["business_error_class"].endswith(".AcquisitionExternalFetchError")
    assert drop["audit_error_class"].endswith(".RuntimeError")
    assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]


@pytest.mark.asyncio
async def test_conversion_rejection_writes_rejected_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """entry 単位の変換棄却 → rejected audit。source failure とは分けて扱う。"""
    source_id = sample_source.id
    handler = ArticleAcquisitionFailureHandler(session_factory)

    await handler.handle_conversion_rejected(source_id, _conversion_rejection())

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "rejected"
    assert ev.outcome_code == "acquisition_conversion_title_missing"
    assert ev.retryability is None
    # title 欠落は責任元 VO 例外を持たない (acquisition 方針違反)。
    # cause 無し → error_class は NULL。
    assert ev.error_class is None
    assert ev.payload["source_name"] == "VentureBeat"
    assert ev.payload["conversion_has_title"] is True
    assert ev.payload["conversion_body_length"] == 42
    assert ev.payload["conversion_has_published_at"] is False


@pytest.mark.asyncio
async def test_conversion_rejection_audit_drop_is_logged_with_secrets_redacted(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """変換棄却 audit が落ちても例外を外へ出さず redacted log に退避する。

    棄却値の観測スナップショットは PII-free なので business 側に free-text は焼かず
    ``business_outcome_code`` のみを残す (secret 混入経路が構造的に消える)。redaction
    の witness は audit 例外 (落ちた監査 DB から漏れうる secret) の方で保つ。
    """
    handler = ArticleAcquisitionFailureHandler(session_factory)
    rejection = _conversion_rejection()

    with (
        patch(
            "app.collection.article_acquisition.failure_handling.SourceAcquisitionAuditRepository"
        ) as mock_audit_cls,
        capture_logs() as cap,
    ):
        mock_audit_cls.return_value.append_conversion_rejected = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )

        await handler.handle_conversion_rejected(sample_source.id, rejection)

    drops = [
        e for e in cap if e.get("event") == "fetched_article_conversion_audit_dropped"
    ]
    assert drops, "conversion rejection fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["source_id"] == sample_source.id
    assert drop["business_outcome_code"] == "acquisition_conversion_title_missing"
    # title 欠落は cause 無し → business_error_class は None。
    assert drop["business_error_class"] is None
    assert drop["audit_error_class"].endswith(".RuntimeError")
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
