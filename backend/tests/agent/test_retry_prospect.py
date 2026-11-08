"""エージェントのAI呼び出しの失敗が呼び直せば解消しうるかの分類。通信せず、分類済みの例外を直接渡す。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.output import AgentOutputDefect, AgentOutputInvalidError
from app.agent.retry_prospect import is_resolvable_by_retry
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
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _transport_error(
    stage: HttpTransportStage, reason: HttpTransportFailureReason
) -> AIProviderTransportError:
    return AIProviderTransportError(
        http_error=HttpTransportError(failure=HttpTransportFailure(stage, reason))
    )


def _response_error(
    reason: AIProviderResponseReason, status_code: int
) -> AIProviderResponseError:
    return AIProviderResponseError(
        reason=reason,
        http_error=HttpResponseError(status_code=status_code, received_at=_RECEIVED_AT),
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            _transport_error(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            ),
            id="transport_timeout",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.RATE_LIMITED, 429),
            id="rate_limited",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.SERVER_ERROR, 503),
            id="server_error",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED),
            id="output_truncated",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.STREAM_INCOMPLETE),
            id="stream_incomplete",
        ),
        pytest.param(
            AgentOutputInvalidError(AgentOutputDefect.OUTPUT_SCHEMA_MISMATCH),
            id="response_invalid",
        ),
    ],
)
def test_transient_or_output_dependent_failure_is_resolvable(
    error: AIProviderError | AgentOutputInvalidError,
) -> None:
    """解消しうる通信の失敗・分あたりの流量制限・サーバーの失敗・出力の打ち切りや途切れ・応答の形の不正は、呼び直せば解消しうる失敗として分類する。"""
    assert is_resolvable_by_retry(error) is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED),
            id="not_configured",
        ),
        pytest.param(
            AIProviderNotSentError(reason=AIProviderNotSentReason.HOST_BLOCKED),
            id="host_blocked",
        ),
        pytest.param(
            _transport_error(
                HttpTransportStage.CONNECT,
                HttpTransportFailureReason.CERTIFICATE_VERIFICATION,
            ),
            id="transport_certificate_verification",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.AUTH, 401),
            id="auth",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.LEAKED_API_KEY, 403),
            id="leaked_api_key",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.PERMISSION_DENIED, 403),
            id="permission_denied",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.NOT_FOUND, 404),
            id="not_found",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.FAILED_PRECONDITION, 400),
            id="failed_precondition",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.INVALID_REQUEST, 400),
            id="invalid_request",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.QUOTA_EXHAUSTED, 429),
            id="quota_exhausted",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.INPUT_TOO_LONG, 400),
            id="input_too_long",
        ),
        pytest.param(
            _response_error(AIProviderResponseReason.INPUT_BLOCKED, 400),
            id="response_input_blocked",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED),
            id="result_input_blocked",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_BLOCKED_SAFETY),
            id="output_blocked_safety",
        ),
        pytest.param(
            AIProviderResultError(
                reason=AIProviderResultReason.OUTPUT_BLOCKED_RECITATION
            ),
            id="output_blocked_recitation",
        ),
        pytest.param(
            AIProviderResultError(
                reason=AIProviderResultReason.OUTPUT_BLOCKED_BLOCKLIST
            ),
            id="output_blocked_blocklist",
        ),
        pytest.param(
            AIProviderResultError(
                reason=AIProviderResultReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT
            ),
            id="output_blocked_prohibited_content",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_BLOCKED_SPII),
            id="output_blocked_spii",
        ),
    ],
)
def test_configuration_quota_or_content_failure_is_not_resolvable(
    error: AIProviderError,
) -> None:
    """未送信・解消しない通信の失敗・設定や権限の誤り・日あたりの利用枠の枯渇・入力や出力の内容による拒否は、呼び直しても解消しない失敗として分類する。"""
    assert is_resolvable_by_retry(error) is False
