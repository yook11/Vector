"""この入力では回復しないAIの失敗だけを受信完了にする判断。"""

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
from app.analysis.ai_provider_settlement import (
    SettledProviderFailure,
    settled_provider_failure,
)
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
def test_not_recoverable_for_input_is_settled(provider_error) -> None:
    """同じ入力では変わらない失敗は、元の例外を持ったまま受信完了にする。"""
    settled = settled_provider_failure(provider_error)

    assert settled == SettledProviderFailure(provider_error)
    assert settled.provider_error is provider_error


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
def test_other_failures_are_left_to_redelivery(provider_error) -> None:
    """入力が原因と断定できない失敗は、受信完了にせず再配信に任せる。"""
    assert settled_provider_failure(provider_error) is None


def test_unclassified_subclass_is_left_to_redelivery() -> None:
    """分類済みでない失敗は、reason が入力の拒否でも受信完了にしない。"""

    class _UnregisteredProviderError(AIProviderError):
        CODE = "unregistered_provider_error"

    error = _UnregisteredProviderError(reason=AIProviderResultReason.INPUT_BLOCKED)

    assert settled_provider_failure(error) is None
