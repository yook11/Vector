"""Consumerの失敗分類が副作用やTaskiqの制御を持たないことを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

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
    classify_curation_failure,
)
from app.analysis.curation.errors import CurationResponseInvalidError
from app.audit.failure_projection import FailureProjection, Retryability
from app.db.errors import (
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseUnexpectedError,
)
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("error", "code", "failure_reason"),
    [
        (
            AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED),
            "ai_provider_not_sent_error",
            "not_configured",
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
            "timeout",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
                http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
            ),
            "ai_provider_response_error",
            "quota_exhausted",
        ),
        (
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_BLOCKED,
                http_error=HttpResponseError(status_code=400, received_at=_RECEIVED_AT),
            ),
            "ai_provider_response_error",
            "input_blocked",
        ),
        (
            AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED),
            "ai_provider_result_error",
            "output_truncated",
        ),
    ],
)
def test_ai_failure_is_projected_from_class_code_and_reason(
    error, code, failure_reason, capsys
) -> None:
    """AIの失敗は包まれずに届き、クラスのcodeとreasonだけを監査へ写す。"""
    assert classify_curation_failure(error) == FailureProjection(
        code=code,
        failure_kind=None,
        failure_reason=failure_reason,
        retryability=None,
        failure_action=None,
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("error", "code", "kind", "retryability"),
    [
        (
            CurationResponseInvalidError(),
            "extraction_response_invalid",
            "ai_response_invalid",
            Retryability.RETRYABLE,
        ),
    ],
)
def test_service_failure_reasons(error, code, kind, retryability) -> None:
    """Service由来の理由をTaskiq例外に変換せず分類する。"""
    projection = classify_curation_failure(error)
    assert projection.code == code
    assert projection.failure_kind == kind
    assert projection.retryability is retryability


@pytest.mark.parametrize(
    ("error", "code", "retryability"),
    [
        (
            DatabaseConnectionError(
                reason=DatabaseConnectionErrorReason.CONNECTION_LOST
            ),
            "db_runtime_error",
            "retryable",
        ),
        (
            DatabaseConstraintError(
                reason=DatabaseConstraintErrorReason.FOREIGN_KEY_VIOLATION
            ),
            "db_constraint_error",
            "non_retryable",
        ),
        (DatabaseUnexpectedError(), "db_unknown_error", "unknown"),
        (OperationalError("query", {}, Exception()), "db_runtime_error", "retryable"),
        (
            IntegrityError("query", {}, Exception()),
            "db_constraint_error",
            "non_retryable",
        ),
        (
            ProgrammingError("query", {}, Exception()),
            "db_query_or_schema_error",
            "non_retryable",
        ),
    ],
)
def test_database_failure_uses_shared_projection(error, code, retryability) -> None:
    """共有DB例外と生のSQLAlchemy例外を既存監査分類へ対応付ける。"""
    projection = classify_curation_failure(error)
    assert projection.code == code
    assert projection.retryability.value == retryability


@pytest.mark.parametrize("error", [RuntimeError("unexpected"), TimeoutError()])
def test_unexpected_failure_and_timeout_are_not_success(error) -> None:
    """想定外例外と時間切れを成功や再配信の抑止に変換しない。"""
    projection = classify_curation_failure(error)
    assert projection.code == "unexpected_error"
    assert projection.retryability is Retryability.UNKNOWN
