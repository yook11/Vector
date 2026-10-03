"""Consumerの失敗から、再配信に任せるか受信完了にするかの判断を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError

from app.ai_providers.errors import (
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.analysis.curation.consumer_failure_classification import (
    NoRetryCuration,
    RetryCuration,
    classify_curation_failure,
)
from app.analysis.curation.errors import CurationResponseInvalidError
from app.db.errors import (
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
)
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_TOO_LONG,
                http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
            ),
            id="input_too_long",
        ),
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_BLOCKED,
                http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
            ),
            id="input_blocked_response",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED),
            id="input_blocked_result",
        ),
    ],
)
def test_failure_unrecoverable_for_input_is_not_retried(error, capsys) -> None:
    """同じ入力では変わらないAIの失敗は、元の例外を持ったまま受信完了にする。"""
    assert classify_curation_failure(error) == NoRetryCuration(error)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED),
            id="ai_not_configured",
        ),
        pytest.param(
            AIProviderTransportError(
                http_error=HttpTransportError(
                    failure=HttpTransportFailure(
                        HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                    )
                )
            ),
            id="ai_transport_timeout",
        ),
        pytest.param(
            AIProviderResponseError(
                reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
            ),
            id="ai_quota_exhausted",
        ),
        pytest.param(
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED),
            id="ai_output_truncated",
        ),
        pytest.param(CurationResponseInvalidError(), id="response_invalid"),
        pytest.param(
            DatabaseConnectionError(
                reason=DatabaseConnectionErrorReason.CONNECTION_LOST
            ),
            id="db_connection_lost",
        ),
        pytest.param(
            DatabaseConstraintError(
                reason=DatabaseConstraintErrorReason.FOREIGN_KEY_VIOLATION
            ),
            id="db_constraint",
        ),
        pytest.param(
            OperationalError("query", {}, Exception()), id="sqlalchemy_operational"
        ),
        pytest.param(RuntimeError("unexpected"), id="unexpected"),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
def test_failure_that_may_recover_or_is_unknown_is_retried(error) -> None:
    """回復しうる失敗と、DB障害・想定外・時間切れは再配信に任せる。"""
    assert classify_curation_failure(error) == RetryCuration(error)
