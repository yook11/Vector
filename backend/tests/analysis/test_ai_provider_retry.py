"""同じ入力では結果が変わらないAIの失敗の判断。"""

from datetime import UTC, datetime

import pytest

from app.ai_providers.errors import (
    AIProviderError,
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.analysis.ai_provider_retry import is_unrecoverable_for_input
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "provider_error",
    [
        AIProviderResponseError(
            reason=AIProviderResponseReason.INPUT_TOO_LONG,
            http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
        ),
        AIProviderResponseError(
            reason=AIProviderResponseReason.INPUT_BLOCKED,
            http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
        ),
        AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED),
    ],
)
def test_failure_caused_by_input_is_unrecoverable(provider_error) -> None:
    """入力の長さや内容で拒否された失敗は、同じ入力では回復しない。"""
    assert is_unrecoverable_for_input(provider_error) is True


@pytest.mark.parametrize(
    "provider_error",
    [
        pytest.param(
            AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED),
            id="not_configured",
        ),
        pytest.param(
            AIProviderTransportError(
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                    )
                )
            ),
            id="transport_timeout",
        ),
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.AUTH,
                http_error=HttpResponseError(status_code=401, received_at=_RECEIVED_AT),
            ),
            id="auth",
        ),
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.INVALID_REQUEST,
                http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
            ),
            id="invalid_request",
        ),
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.RATE_LIMITED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
            ),
            id="rate_limited",
        ),
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.SERVER_ERROR,
                http_error=HttpResponseError(status_code=503, received_at=_RECEIVED_AT),
            ),
            id="server_error",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_BLOCKED_SAFETY),
            id="output_blocked",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED),
            id="output_truncated",
        ),
    ],
)
def test_failure_not_attributable_to_input_may_recover(provider_error) -> None:
    """入力が原因と断定できない失敗は、回復しないとは判断しない。"""
    assert is_unrecoverable_for_input(provider_error) is False


def test_unclassified_subclass_may_recover() -> None:
    """分類済みでない失敗は、reason が入力の拒否でも回復しないとは判断しない。"""

    class _UnregisteredProviderError(AIProviderError):
        CODE = "unregistered_provider_error"

    error = _UnregisteredProviderError(reason=AIProviderResultReason.INPUT_BLOCKED)

    assert is_unrecoverable_for_input(error) is False
