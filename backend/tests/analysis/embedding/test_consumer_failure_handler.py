"""Consumerの失敗後処理を既存の計測・通知の出力で検証する(DB不要)。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderTransportError,
)
from app.analysis.embedding.consumer_failure_handling import (
    EmbeddingConsumerFailureHandler,
)
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingResponseInvalidError,
)
from app.analysis.logging import create_article_analysis_logger
from app.db.errors import DatabaseUnexpectedError
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)
from tests.cloudwatch.records import metric_records

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_HANDLER = "app.analysis.embedding.consumer_failure_handling"


@pytest.fixture
def embedding_logger():
    return create_article_analysis_logger().bind(stage="embedding")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "notice_kind"),
    [
        (EmbeddingAnalyzedArticleMissingError(), None),
        (EmbeddingResponseInvalidError(), None),
        (
            AIProviderTransportError(
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                    )
                )
            ),
            None,
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.RATE_LIMITED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
            ),
            None,
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
            ),
            "quota_exhausted",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.INSUFFICIENT_BALANCE,
                http_error=HttpResponseError(status_code=402, received_at=_RECEIVED_AT),
            ),
            "insufficient_balance",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_TOO_LONG,
                http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
            ),
            None,
        ),
        (DatabaseUnexpectedError(), None),
        (RuntimeError("Authorization: Bearer test-secret-do-not-record"), None),
    ],
)
async def test_records_failure_without_changing_original_error(
    error,
    notice_kind,
    capsys,
    embedding_logger,
) -> None:
    """失敗件数と必要な枯渇通知を記録し、元の例外をそのまま返せる。"""
    cause = error.__cause__
    with pytest.raises(type(error)) as raised:
        try:
            raise error
        except Exception as caught:
            result = await EmbeddingConsumerFailureHandler().handle(
                exc=caught,
                provider="gemini",
                logger=embedding_logger,
            )
            assert result is None
            raise
    assert raised.value is error
    assert error.__cause__ is cause
    output = capsys.readouterr().out
    outcomes = metric_records(output, "processing_outcome")
    assert len(outcomes) == 1
    assert outcomes[0]["result"] == "failed"
    notices = metric_records(output, "ai_provider_exhausted")
    if notice_kind is not None:
        assert len(notices) == 1
        assert notices[0]["kind"] == notice_kind
        assert notices[0]["provider"] == "gemini"
    else:
        assert notices == []


@pytest.mark.asyncio
async def test_metric_failure_does_not_prevent_notification(embedding_logger) -> None:
    """計測と通知が失敗しても例外を外へ出さず、通知を試みる。"""
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
        await EmbeddingConsumerFailureHandler().handle(
            exc=error,
            provider="gemini",
            logger=embedding_logger,
        )
    notify.assert_called_once_with(error, provider="gemini")
