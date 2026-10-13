"""``ArticleAcquisitionFailureRecorder`` の監査記録テスト。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_acquisition.consumer_result import (
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.article_acquisition.errors import (
    ResponseSizeLimitExceededError,
    RssFeedErrors,
    RssFeedFailure,
)
from app.collection.article_acquisition.failure_recording import (
    ArticleAcquisitionFailureRecorder,
)
from app.collection.article_acquisition.fetched_article import (
    FetchedArticleTitleMissingError,
)
from app.collection.article_acquisition.raw_article import RawArticle
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.domain.article_url import ArticleUrlInvalidError
from app.collection.response_size import ResponseSizeBasis
from app.db.errors import DatabaseTimeoutError, DatabaseTimeoutErrorReason
from app.http.destination_policy import (
    DestinationRejectedError,
    DestinationRejectedReason,
)
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent
from app.shared.web_url import WebUrlInvalidReason


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


def _title_missing_raw_article() -> RawArticle:
    """空白だけの title で、変換が title 欠落として棄却する記事。"""
    return RawArticle(
        title="   ",
        url="https://venturebeat.com/rejected",
        body="x" * 42,
        published_at=None,
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
            ),
            code="http_transport_error",
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
async def test_destination_rejected_records_without_exception_message(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """宛先拒否は再試行不可として記録し、拒否した宛先を監査に載せない。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_source_failure(
        source_id=source_id,
        source_name="VentureBeat",
        failure=NoRetryAcquisition(
            DestinationRejectedError(
                reason=DestinationRejectedReason.NON_PUBLIC_IP_LITERAL,
                host="10.0.0.1",
                address="10.0.0.1",
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
async def test_response_size_limit_records_numbers_without_retry(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """本文の上限超過は再試行不可として記録し、上限・確認した量・根拠だけを残す。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_source_failure(
        source_id=source_id,
        source_name="Anthropic",
        failure=NoRetryAcquisition(
            ResponseSizeLimitExceededError(
                limit_bytes=10485760,
                observed_bytes=10485761,
                size_basis=ResponseSizeBasis.DECLARED_CONTENT_LENGTH,
            )
        ),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "source_response_size_limit_exceeded"
    assert ev.retryability == "non_retryable"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".ResponseSizeLimitExceededError")
    assert ev.payload["failure_kind"] == "response_size_limit_exceeded"
    assert ev.payload["failure_action"] == "no_retry"
    assert ev.payload["error_message"] == (
        "source_response_size_limit_exceeded: observed=10485761 limit=10485760"
        " basis=declared_content_length"
    )
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
            ),
            code="rss_feed_errors",
            requires_investigation=True,
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
        failure=RetryAcquisition(RuntimeError("boom"), code="unknown"),
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
            DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.STATEMENT_TIMEOUT),
            code="unknown",
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
            failure=RetryAcquisition(RuntimeError("original failure"), code="unknown"),
        )


@pytest.mark.asyncio
async def test_audit_failure_falls_back_to_log_with_secrets_redacted(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """監査の保存が落ちても記録係は完走し、業務の例外型と、秘密を伏せた監査の例外をログに残す。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    business_exc = ValueError("blocked Authorization: Bearer sk-live-BUSINESSSECRETabc")

    with patch(
        "app.collection.article_acquisition.failure_recording.SourceAcquisitionAuditRepository"
    ) as mock_audit_cls:
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
        log
        for log in map(json.loads, capsys.readouterr().out.splitlines())
        if log["event"] == "source_acquisition_failure_audit_dropped"
    ]
    for drop in drops:
        del drop["timestamp"]
        del drop["frames"]
    assert drops == [
        {
            "event": "source_acquisition_failure_audit_dropped",
            "level": "warning",
            "log_policy": "external_content_fetch",
            "operation": "audit",
            "source_id": source_id,
            "business_error_class": "builtins.ValueError",
            "error_class": "builtins.RuntimeError",
            "error_message": "audit db down Authorization: [redacted:credential]",
        }
    ]


@pytest.mark.asyncio
async def test_conversion_rejection_writes_rejected_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """title 欠落の例外を渡すと、理由と記事の状態を持つ rejected 行が残る。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_conversion_rejected(
        source_id=source_id,
        source_name="VentureBeat",
        raw_article=_title_missing_raw_article(),
        error=FetchedArticleTitleMissingError(),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "rejected"
    assert ev.outcome_code == "acquisition_conversion_title_missing"
    assert ev.retryability is None
    assert ev.error_class == (
        "app.collection.article_acquisition.fetched_article.FetchedArticleTitleMissingError"
    )
    assert {
        key: value
        for key, value in ev.payload.items()
        if key == "source_name" or key.startswith("conversion_")
    } == {
        "source_name": "VentureBeat",
        "conversion_raw_url": "https://venturebeat.com/rejected",
        "conversion_has_title": True,
        "conversion_body_length": 42,
        "conversion_has_published_at": False,
    }


@pytest.mark.asyncio
async def test_conversion_rejection_with_blank_url_records_no_raw_url(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """空白だけの URL で棄却された記事は、監査の元の URL を空として残す。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_conversion_rejected(
        source_id=source_id,
        source_name="VentureBeat",
        raw_article=RawArticle(
            title="Has Title", url="   ", body=None, published_at=None
        ),
        error=ArticleUrlInvalidError(reason=WebUrlInvalidReason.URL_EMPTY),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert [(ev.outcome_code, ev.payload["conversion_raw_url"]) for ev in events] == [
        ("url_empty", None)
    ]


@pytest.mark.asyncio
async def test_unexpected_conversion_error_records_unexpected_outcome(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """想定外の例外で棄却された記事は、想定外のコードと元の例外の型で監査される。"""
    source_id = sample_source.id
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    await recorder.record_conversion_rejected(
        source_id=source_id,
        source_name="VentureBeat",
        raw_article=_title_missing_raw_article(),
        error=RuntimeError("post-precondition invariant violation"),
    )

    await db_session.rollback()
    events = await _fetch_acquisition_events(db_session, source_id)
    assert [(ev.outcome_code, ev.error_class) for ev in events] == [
        ("acquisition_conversion_unexpected_error", "builtins.RuntimeError")
    ]


@pytest.mark.asyncio
async def test_conversion_rejection_audit_drop_is_logged_with_secrets_redacted(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """変換棄却の監査が落ちても例外を外へ出さず、棄却の例外型と、秘密を伏せた監査の例外をログに残す。"""
    recorder = ArticleAcquisitionFailureRecorder(session_factory)

    with patch(
        "app.collection.article_acquisition.failure_recording.SourceAcquisitionAuditRepository"
    ) as mock_audit_cls:
        mock_audit_cls.return_value.append_conversion_rejected = AsyncMock(
            side_effect=RuntimeError(
                "audit db down Authorization: Bearer sk-live-AUDITSECRETxyz"
            )
        )

        await recorder.record_conversion_rejected(
            source_id=sample_source.id,
            source_name="VentureBeat",
            raw_article=_title_missing_raw_article(),
            error=FetchedArticleTitleMissingError(),
        )

    drops = [
        log
        for log in map(json.loads, capsys.readouterr().out.splitlines())
        if log["event"] == "fetched_article_conversion_audit_dropped"
    ]
    for drop in drops:
        del drop["timestamp"]
        del drop["frames"]
    assert drops == [
        {
            "event": "fetched_article_conversion_audit_dropped",
            "level": "warning",
            "log_policy": "external_content_fetch",
            "operation": "audit",
            "source_id": sample_source.id,
            "business_error_class": (
                "app.collection.article_acquisition.fetched_article."
                "FetchedArticleTitleMissingError"
            ),
            "error_class": "builtins.RuntimeError",
            "error_message": "audit db down Authorization: [redacted:credential]",
        }
    ]
