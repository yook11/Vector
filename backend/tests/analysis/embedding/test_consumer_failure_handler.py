"""Consumerの失敗後処理を実DBと既存の通知出力で検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderTransportError,
)
from app.analysis.embedding.consumer_failure_classification import (
    classify_embedding_failure,
)
from app.analysis.embedding.consumer_failure_handling import (
    EmbeddingConsumerFailureHandler,
)
from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildRejected,
    EmbeddingReadyBuildRejectionReason,
)
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingResponseInvalidError,
)
from app.analysis.logging import create_article_analysis_logger
from app.audit.stages.embedding import EmbeddingAuditRepository
from app.db.errors import (
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseUnexpectedError,
)
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent
from tests.cloudwatch.records import metric_records

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_HANDLER = "app.analysis.embedding.consumer_failure_handling"


@pytest.fixture
def embedding_logger():
    return create_article_analysis_logger().bind(stage="embedding")


@pytest.fixture
async def article_id(db_session: AsyncSession, sample_source: NewsSource) -> int:
    article = AnalyzableArticleRecord(
        source_id=sample_source.id,
        source_url="https://example.com/consumer-failure",
        original_title="title",
        original_content="content",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    return article.id


async def _events(session: AsyncSession) -> list[PipelineEvent]:
    return list((await session.execute(select(PipelineEvent))).scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "failure_kind", "failure_reason", "failure_action", "notified"),
    [
        (
            EmbeddingAnalyzedArticleMissingError(),
            "embedding_analyzed_article_missing",
            "target_missing",
            None,
            "no_retry",
            False,
        ),
        (
            EmbeddingResponseInvalidError(),
            "embedding_response_invalid",
            "ai_response_invalid",
            None,
            "retry",
            False,
        ),
        (
            AIProviderTransportError(
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                    )
                )
            ),
            "ai_provider_transport_error",
            None,
            "timeout",
            "retry",
            False,
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.RATE_LIMITED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
            ),
            "ai_provider_response_error",
            None,
            "rate_limited",
            "retry",
            False,
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
            ),
            "ai_provider_response_error",
            None,
            "quota_exhausted",
            "retry",
            True,
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.INSUFFICIENT_BALANCE,
                http_error=HttpResponseError(status_code=402, received_at=_RECEIVED_AT),
            ),
            "ai_provider_response_error",
            None,
            "insufficient_balance",
            "retry",
            True,
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_TOO_LONG,
                http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
            ),
            "ai_provider_response_error",
            None,
            "input_too_long",
            "no_retry",
            False,
        ),
        (
            DatabaseUnexpectedError(),
            "db_unknown_error",
            "db_unknown",
            None,
            "retry",
            False,
        ),
        (
            RuntimeError("Authorization: Bearer test-secret-do-not-record"),
            "unexpected_error",
            "unknown",
            None,
            "retry",
            False,
        ),
    ],
)
async def test_records_failure_and_decision_without_changing_original_error(
    db_session,
    session_factory,
    article_id,
    error,
    code,
    failure_kind,
    failure_reason,
    failure_action,
    notified,
    capsys,
    embedding_logger,
) -> None:
    """監査・失敗件数・必要な枯渇通知を記録し、元の例外をそのまま返せる。"""
    cause = error.__cause__
    with pytest.raises(type(error)) as raised:
        try:
            raise error
        except Exception as caught:
            result = await EmbeddingConsumerFailureHandler(session_factory).handle(
                failure=classify_embedding_failure(caught),
                exc=caught,
                analyzed_article_id=123,
                analyzable_article_id=article_id,
                provider="gemini",
                logger=embedding_logger,
            )
            assert result is None
            raise
    assert raised.value is error
    assert error.__cause__ is cause
    events = await _events(db_session)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "failed"
    assert event.outcome_code == code
    assert event.retryability is None
    assert event.payload["failure_kind"] == failure_kind
    assert event.payload["failure_reason"] == failure_reason
    assert event.payload["failure_action"] == failure_action
    assert event.payload["analyzed_article_id"] == 123
    assert event.article_id == article_id
    assert event.error_class == f"{type(error).__module__}.{type(error).__qualname__}"
    assert event.payload["error_chain"][0] == event.error_class
    assert "test-secret-do-not-record" not in str(event.payload)
    output = capsys.readouterr().out
    outcomes = metric_records(output, "processing_outcome")
    assert len(outcomes) == 1
    assert outcomes[0]["result"] == "failed"
    notices = metric_records(output, "ai_provider_exhausted")
    if notified:
        assert len(notices) == 1
        assert notices[0]["kind"] == failure_reason
        assert notices[0]["provider"] == "gemini"
    else:
        assert notices == []


@pytest.mark.asyncio
async def test_audit_failure_does_not_prevent_notification(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    capsys,
    embedding_logger,
) -> None:
    """実DBの外部キー違反で監査が失敗しても枯渇通知を試みる。"""
    error = AIProviderResponseError(
        reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
        http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
    )
    await EmbeddingConsumerFailureHandler(session_factory).handle(
        failure=classify_embedding_failure(error),
        exc=error,
        analyzed_article_id=123,
        analyzable_article_id=999_999,
        provider="gemini",
        logger=embedding_logger,
    )
    assert await _events(db_session) == []
    notices = metric_records(capsys.readouterr().out, "ai_provider_exhausted")
    assert len(notices) == 1
    assert notices[0]["provider"] == "gemini"


@pytest.mark.asyncio
async def test_notification_and_metric_failures_do_not_prevent_audit(
    db_session, session_factory, article_id, embedding_logger
) -> None:
    """通知と計測が失敗しても監査を保存し、通知を試みる。"""
    error = AIProviderResponseError(
        reason=AIProviderResponseReason.INSUFFICIENT_BALANCE,
        http_error=HttpResponseError(status_code=402, received_at=_RECEIVED_AT),
    )
    with (
        patch(
            f"{_HANDLER}.record_embedding_processing_outcome",
            side_effect=RuntimeError("metric-secret"),
        ),
        patch(
            f"{_HANDLER}.record_ai_provider_exhausted",
            side_effect=RuntimeError("notification-secret"),
        ) as notify,
    ):
        await EmbeddingConsumerFailureHandler(session_factory).handle(
            failure=classify_embedding_failure(error),
            exc=error,
            analyzed_article_id=123,
            analyzable_article_id=article_id,
            provider="gemini",
            logger=embedding_logger,
        )
    assert len(await _events(db_session)) == 1
    notify.assert_called_once_with(error, provider="gemini")


@pytest.mark.asyncio
async def test_secondary_reporting_failure_preserves_original_and_notification(
    db_session, session_factory, capsys, embedding_logger
) -> None:
    """監査とdrop計測が失敗しても元の例外と通知を維持する。"""
    error = AIProviderResponseError(
        reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
        http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
    )
    with (
        patch(
            f"{_HANDLER}.record_audit_dropped", side_effect=RuntimeError("drop failed")
        ),
        pytest.raises(type(error)) as raised,
    ):
        try:
            raise error
        except Exception:
            await EmbeddingConsumerFailureHandler(session_factory).handle(
                failure=classify_embedding_failure(error),
                exc=error,
                analyzed_article_id=123,
                analyzable_article_id=999_999,
                provider="gemini",
                logger=embedding_logger,
            )
            raise
    assert raised.value is error
    assert await _events(db_session) == []
    assert len(metric_records(capsys.readouterr().out, "ai_provider_exhausted")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("secondary_failure", ["none", "metric"])
async def test_rejection_audit_failure_does_not_escape_handler(
    db_session, session_factory, secondary_failure, embedding_logger
):
    """拒否監査と診断の通常障害を抑止し、未確定の監査を残さない。"""
    rejected = EmbeddingReadyBuildRejected(
        EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING
    )
    append = EmbeddingAuditRepository.append_ready_build_rejected

    async def append_then_fail(repo, **kwargs):
        await append(repo, **kwargs)
        raise RuntimeError("private-audit-details")

    with (
        patch.object(
            EmbeddingAuditRepository,
            "append_ready_build_rejected",
            new=append_then_fail,
        ),
        patch(f"{_HANDLER}.record_audit_dropped") as dropped,
    ):
        if secondary_failure == "metric":
            dropped.side_effect = RuntimeError("private-metric-details")
        await EmbeddingConsumerFailureHandler(
            session_factory
        ).handle_ready_build_rejected(
            analyzed_article_id=999_999, rejected=rejected, logger=embedding_logger
        )

    assert await _events(db_session) == []
    dropped.assert_called_once()


@pytest.mark.asyncio
async def test_database_failure_audit_preserves_classification_with_null_message(
    db_session, session_factory, article_id, embedding_logger
):
    """DB例外の文面が空でも実DBへ分類と原因チェーンを保存する。"""
    from sqlalchemy.exc import OperationalError

    cause = OperationalError(None, None, RuntimeError("private database details"))
    error = DatabaseConnectionError(
        reason=DatabaseConnectionErrorReason.CONNECTION_LOST
    )
    error.__cause__ = cause

    await EmbeddingConsumerFailureHandler(session_factory).handle(
        failure=classify_embedding_failure(error),
        exc=error,
        analyzed_article_id=123,
        analyzable_article_id=article_id,
        provider="gemini",
        logger=embedding_logger,
    )

    (event,) = await _events(db_session)
    assert event.payload["error_message"] is None
    assert event.outcome_code == "db_runtime_error"
    assert event.retryability is None
    assert event.payload["failure_kind"] == "db_runtime"
    assert event.payload["failure_action"] == "retry"
    assert event.error_class == "app.db.errors.DatabaseConnectionError"
    assert event.payload["error_chain"] == [
        "app.db.errors.DatabaseConnectionError",
        "sqlalchemy.exc.OperationalError",
    ]
