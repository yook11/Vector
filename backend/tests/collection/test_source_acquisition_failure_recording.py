"""``ArticleAcquisitionFailureRecorder`` の監査記録テスト。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.collection.article_acquisition.consumer_failure_classification import (
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.article_acquisition.errors import RssFeedErrors, RssFeedFailure
from app.collection.article_acquisition.failure_recording import (
    ArticleAcquisitionFailureRecorder,
)
from app.collection.article_acquisition.fetched_article_converter import (
    AcquisitionConversionRejection,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.db.errors import DatabaseTimeoutError, DatabaseTimeoutErrorReason
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
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


def _conversion_rejection() -> AcquisitionConversionRejection:
    """title 欠落の棄却値 (acquisition 所有の reason、cause 無し)。"""
    return AcquisitionConversionRejection(
        outcome_code="acquisition_conversion_title_missing",
        source_name="VentureBeat",
        raw_url="https://venturebeat.com/rejected",
        has_title=True,
        body_length=42,
        has_published_at=False,
        cause=None,
    )


_RECEIVED = datetime(2026, 9, 26, tzinfo=UTC)


@pytest.mark.asyncio
async def test_http_response_failure_records_judgement_code_and_status(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """非成功応答は外部取得の失敗判断の code・再試行可否で記録し、status を別列に残す。

    生の Retry-After は監査へ載せない。
    """
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_source_failure(
        source_id=source_id,
        source_name="VentureBeat",
        failure=NoRetryAcquisition(
            HttpResponseError(status_code=403, received_at=_RECEIVED, retry_after="120")
        ),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "http_response_error"
    assert ev.retryability == "non_retryable"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".HttpResponseError")
    assert ev.payload["source_name"] == "VentureBeat"
    assert ev.payload["failure_kind"] == "external_fetch"
    assert ev.payload["failure_action"] == "no_retry"
    assert ev.payload["http_status"] == 403
    assert ev.payload["reason_code"] is None
    assert ev.payload["error_message"] is None
    assert ev.payload["read_format"] is None
    assert "120" not in str(ev.payload)


@pytest.mark.asyncio
async def test_transport_failure_records_reason_as_retryable(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """通信失敗は再試行可能として記録し、段階を除いた理由を reason_code に残す。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_source_failure(
        source_id=source_id,
        source_name="VentureBeat",
        failure=RetryAcquisition(
            HttpTransportError(
                failure=HttpTransportFailure(
                    HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                )
            )
        ),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "http_transport_error"
    assert ev.retryability == "retryable"
    assert ev.payload["failure_action"] == "retry"
    assert ev.payload["failure_kind"] == "external_fetch"
    assert ev.payload["reason_code"] == "timeout"
    assert ev.payload["http_status"] is None


@pytest.mark.asyncio
async def test_host_blocked_records_without_exception_message(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """宛先拒否は再試行不可として記録し、宛先やsecretを含みうる自由文を載せない。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_source_failure(
        source_id=source_id,
        source_name="VentureBeat",
        failure=NoRetryAcquisition(
            HostBlockedError(
                "host is non-public IP literal: 10.0.0.1 "
                "Bearer sk-live-SSRFSECRETvalue123"
            )
        ),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "host_blocked"
    assert ev.retryability == "non_retryable"
    assert ev.payload["failure_action"] == "no_retry"
    assert ev.payload["error_message"] is None
    assert "10.0.0.1" not in str(ev.payload)
    assert "SSRFSECRET" not in str(ev.payload)


@pytest.mark.asyncio
async def test_read_failure_writes_reason_outcome_and_read_payload(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """読取失敗は reason.value を outcome_code に、読取の位置を ``read_*`` に焼き、
    explicit message ではなく PII-free な既定メッセージを error_message に使う。
    """
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_source_failure(
        source_id=source_id,
        source_name="VentureBeat",
        failure=NoRetryAcquisition(
            UnreadableResponseError(
                "private body sk-live-READSECRETvalue123",
                reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
                response_format="json",
                field="items",
            )
        ),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "read_unexpected_field_shape"
    assert ev.retryability == "non_retryable"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".UnreadableResponseError")
    assert ev.payload["failure_kind"] == "unreadable_response"
    assert ev.payload["failure_action"] == "no_retry"
    assert (
        ev.payload["error_message"] == "read_unexpected_field_shape: json field=items"
    )
    assert ev.payload["read_format"] == "json"
    assert ev.payload["read_field"] == "items"
    assert ev.payload["read_parser_position"] is None
    assert ev.payload["http_status"] is None
    assert ev.payload["reason_code"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_status", "retryability", "failure_type", "failure_action"),
    [
        (503, "retryable", RetryAcquisition, "retry"),
        (404, "non_retryable", NoRetryAcquisition, "no_retry"),
    ],
)
async def test_rss_feed_errors_record_every_feed_and_any_retryable(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    second_status: int,
    retryability: str,
    failure_type: type[RetryAcquisition | NoRetryAcquisition],
    failure_action: str,
) -> None:
    """全フィード失敗は各フィードの原因を秘匿規則付きで残し、どれか1つでも
    再試行可能なら全体を再試行可能として記録する。
    """
    secret = "sk-" + "x" * 24
    cause = ValueError(f"raw response {secret}")
    read_error = UnreadableResponseError(
        f"private body {secret}",
        reason=UnreadableResponseReason.MALFORMED_CONTENT,
        response_format="feed",
    )
    read_error.__cause__ = cause
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_source_failure(
        source_id=source_id,
        source_name="VentureBeat",
        failure=failure_type(
            RssFeedErrors(
                [
                    RssFeedFailure(
                        feed_url=f"https://example.com/feed?key={secret}",
                        error=read_error,
                    ),
                    RssFeedFailure(
                        feed_url="https://example.com/other",
                        error=HttpResponseError(
                            status_code=second_status, received_at=_RECEIVED
                        ),
                    ),
                ]
            )
        ),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "rss_feed_errors"
    assert ev.retryability == retryability
    assert ev.payload["failure_action"] == failure_action
    assert ev.payload["failure_kind"] == "rss_feeds"
    assert secret not in str(ev.payload)
    assert "private body" not in str(ev.payload)
    assert "raw response" not in str(ev.payload)
    first, second = ev.payload["feed_failures"]
    assert first["code"] == "read_malformed_content"
    assert first["error_message"] == "read_malformed_content: feed"
    assert first["error_chain"] == [
        f"{UnreadableResponseError.__module__}.UnreadableResponseError",
        "builtins.ValueError",
    ]
    assert first["http_status"] is None
    assert second == {
        "feed_url": "https://example.com/other",
        "code": "http_response_error",
        "error_class": "app.http.errors.HttpResponseError",
        "error_message": None,
        "error_chain": ["app.http.errors.HttpResponseError"],
        "http_status": second_status,
        "reason_code": None,
    }


@pytest.mark.asyncio
async def test_unexpected_error_records_unknown_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """想定外 ``Exception`` → unexpected_error audit。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    result = await recorder.record_source_failure(
        source_id=source_id,
        source_name="VentureBeat",
        failure=RetryAcquisition(RuntimeError("boom")),
    )

    assert result is None
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
    assert ev.payload["failure_action"] == "retry"


@pytest.mark.asyncio
async def test_database_failure_records_retryability(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """DB障害の監査は再試行可能な失敗として保存する。"""
    recorder = ArticleAcquisitionFailureRecorder(session_factory)
    result = await recorder.record_source_failure(
        source_id=sample_source.id,
        source_name="VentureBeat",
        failure=RetryAcquisition(
            DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.STATEMENT_TIMEOUT)
        ),
    )

    assert result is None
    events = await _fetch_acquisition_events(db_session, sample_source.id)
    assert len(events) == 1
    assert events[0].event_type == "failed"
    assert events[0].outcome_code == "db_runtime_error"
    assert events[0].retryability == "retryable"


@pytest.mark.asyncio
async def test_cancellation_during_failure_audit_propagates(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """監査commit時のキャンセルは通常の監査障害として握り潰さない。"""

    def cancel_commit(session):
        raise asyncio.CancelledError()

    @asynccontextmanager
    async def cancelled_audit_session():
        async with session_factory() as session:
            event.listen(session.sync_session, "before_commit", cancel_commit)
            yield session

    recorder = ArticleAcquisitionFailureRecorder(cancelled_audit_session)
    with pytest.raises(asyncio.CancelledError):
        await recorder.record_source_failure(
            source_id=sample_source.id,
            source_name="VentureBeat",
            failure=RetryAcquisition(RuntimeError("original failure")),
        )


@pytest.mark.asyncio
async def test_audit_failure_falls_back_to_log_with_secrets_redacted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """audit Repository が落ちても recorder は完走し redacted log に退避する。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    business_exc = HostBlockedError(
        "blocked Authorization: Bearer sk-live-BUSINESSSECRETabc"
    )

    with (
        patch(
            "app.collection.article_acquisition.failure_recording.SourceAcquisitionAuditRepository"
        ) as mock_audit_cls,
        capture_logs() as cap,
    ):
        mock_audit_cls.return_value.append_failure = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )
        result = await recorder.record_source_failure(
            source_id=source_id,
            source_name="VentureBeat",
            failure=NoRetryAcquisition(business_exc),
        )

    assert result is None
    drops = [
        e for e in cap if e.get("event") == "source_acquisition_failure_audit_dropped"
    ]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["source_id"] == source_id
    assert drop["business_error_class"].endswith(".HostBlockedError")
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
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_conversion_rejected(source_id, _conversion_rejection())

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
    recorder = ArticleAcquisitionFailureRecorder(session_factory)
    rejection = _conversion_rejection()

    with (
        patch(
            "app.collection.article_acquisition.failure_recording.SourceAcquisitionAuditRepository"
        ) as mock_audit_cls,
        capture_logs() as cap,
    ):
        mock_audit_cls.return_value.append_conversion_rejected = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )

        await recorder.record_conversion_rejected(sample_source.id, rejection)

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
