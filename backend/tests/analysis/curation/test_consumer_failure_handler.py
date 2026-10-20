"""Consumerの失敗後処理を既存の計測・通知の出力で検証する(DB不要)。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResponseReason,
)
from app.analysis.curation.consumer_failure_handling import (
    CurationConsumerFailureHandler,
)
from app.analysis.curation.errors import CurationResponseInvalidError
from app.analysis.logging import create_article_analysis_logger
from app.http.errors import HttpResponseError
from tests.cloudwatch.records import metric_records

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_HANDLER = "app.analysis.curation.consumer_failure_handling"


@pytest.fixture
def curation_logger():
    return create_article_analysis_logger().bind(stage="curation")


@pytest.mark.asyncio
async def test_successful_handling_records_failed_outcome_and_notifies(
    capsys, curation_logger
) -> None:
    """後処理が成功すれば処理失敗件数を残し、枯渇なら通知する。"""
    error = AIProviderResponseError(
        reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
        http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
    )
    await CurationConsumerFailureHandler().handle(
        exc=error,
        provider="gemini",
        logger=curation_logger,
    )
    output = capsys.readouterr().out
    outcomes = metric_records(output, "processing_outcome")
    assert len(outcomes) == 1
    assert outcomes[0]["result"] == "failed"
    notices = metric_records(output, "ai_provider_exhausted")
    assert len(notices) == 1
    assert notices[0]["provider"] == "gemini"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AIProviderResponseError(
            reason=AIProviderResponseReason.RATE_LIMITED,
            http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
        ),
        CurationResponseInvalidError(),
    ],
)
async def test_failure_other_than_exhaustion_is_recorded_without_notification(
    capsys, error, curation_logger
) -> None:
    """枯渇以外の失敗は処理失敗件数だけを残し、枯渇通知を出さない。"""
    await CurationConsumerFailureHandler().handle(
        exc=error,
        provider="gemini",
        logger=curation_logger,
    )
    output = capsys.readouterr().out
    outcomes = metric_records(output, "processing_outcome")
    assert [record["result"] for record in outcomes] == ["failed"]
    assert metric_records(output, "ai_provider_exhausted") == []


@pytest.mark.asyncio
async def test_metric_failure_does_not_prevent_notification(curation_logger) -> None:
    """計測と通知が失敗しても例外を外へ出さず、通知を試みる。"""
    error = AIProviderResponseError(
        reason=AIProviderResponseReason.INSUFFICIENT_BALANCE,
        http_error=HttpResponseError(status_code=402, received_at=_RECEIVED_AT),
    )
    with (
        patch(
            f"{_HANDLER}.record_curation_processing_outcome",
            side_effect=RuntimeError("metric-secret"),
        ),
        patch(
            f"{_HANDLER}.record_ai_provider_exhausted",
            side_effect=RuntimeError("notification-secret"),
        ) as notify,
    ):
        await CurationConsumerFailureHandler().handle(
            exc=error,
            provider="gemini",
            logger=curation_logger,
        )
    notify.assert_called_once_with(error, provider="gemini")
