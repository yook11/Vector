"""Consumerの失敗分類が副作用やTaskiqの制御を持たないことを検証する。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderOutputTruncatedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.ai_providers.gemini.error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)
from app.analysis.embedding.consumer_failure_classification import (
    classify_embedding_failure,
)
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingResponseInvalidError,
    to_embedding_error,
)
from app.audit.failure_projection import Retryability
from app.db.errors import (
    DatabaseConnectionError,
    DatabaseConnectionErrorReason,
    DatabaseConstraintError,
    DatabaseConstraintErrorReason,
    DatabaseUnexpectedError,
)


@pytest.mark.parametrize(
    ("provider_error", "notified"),
    [
        (
            AIProviderNetworkError(reason=GeminiStateReason.TIMEOUT),
            False,
        ),
        (
            AIProviderServiceUnavailableError(),
            False,
        ),
        (
            AIProviderRateLimitedError(),
            False,
        ),
        (
            AIProviderUsageLimitExhaustedError(),
            True,
        ),
        (
            AIProviderInsufficientBalanceError(),
            True,
        ),
        (
            AIProviderConfigurationError(),
            False,
        ),
        (
            AIProviderRequestInvalidError(),
            False,
        ),
        (
            AIProviderOutputTruncatedError(),
            False,
        ),
        (
            AIProviderInputRejectedError(reason=GeminiContentRejectionReason.SAFETY),
            False,
        ),
        (
            AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
            False,
        ),
    ],
)
def test_provider_classification_preserves_existing_audit_and_notification(
    provider_error, notified, capsys
) -> None:
    """全provider分類で監査コード・原因詳細・枯渇通知対象を維持する。"""
    error = to_embedding_error(provider_error)
    failure = classify_embedding_failure(error)
    assert failure.audit.code == provider_error.CODE
    assert failure.audit.failure_kind is None
    assert failure.audit.retryability is None
    assert failure.audit.failure_reason == (
        provider_error.reason.value if provider_error.reason is not None else None
    )
    assert failure.audit.failure_action is None
    assert failure.provider_exhaustion is (provider_error if notified else None)
    assert classify_embedding_failure(error) == failure
    assert not hasattr(failure, "reraise")
    assert error.__cause__ is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("error", "code", "kind", "retryability"),
    [
        (
            EmbeddingAnalyzedArticleMissingError(),
            "embedding_analyzed_article_missing",
            "target_missing",
            Retryability.NON_RETRYABLE,
        ),
        (
            EmbeddingResponseInvalidError(),
            "embedding_response_invalid",
            "ai_response_invalid",
            Retryability.RETRYABLE,
        ),
    ],
)
def test_service_failure_reasons(error, code, kind, retryability) -> None:
    """Service由来の理由をTaskiq例外に変換せず分類する。"""
    failure = classify_embedding_failure(error)
    assert failure.audit.code == code
    assert failure.audit.failure_kind == kind
    assert failure.audit.retryability is retryability
    assert failure.provider_exhaustion is None


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
    failure = classify_embedding_failure(error)
    assert failure.audit.code == code
    assert failure.audit.retryability.value == retryability
    assert failure.provider_exhaustion is None


@pytest.mark.parametrize("error", [RuntimeError("unexpected"), TimeoutError()])
def test_unexpected_failure_and_timeout_are_not_success(error) -> None:
    """想定外例外と時間切れを成功や再配信の抑止に変換しない。"""
    failure = classify_embedding_failure(error)
    assert failure.audit.code == "unexpected_error"
    assert failure.audit.retryability is Retryability.UNKNOWN
    assert failure.provider_exhaustion is None


@pytest.mark.parametrize(
    "error_type", [AIProviderInputRejectedError, AIProviderOutputBlockedError]
)
def test_rejection_without_reason_has_nullable_audit_details(error_type) -> None:
    """理由を省略した拒否でも監査コードを保持し、詳細を補完しない。"""
    provider = error_type("provider diagnostic")
    error = to_embedding_error(provider)
    failure = classify_embedding_failure(error)
    assert failure.audit.code == provider.CODE
    assert failure.audit.failure_reason is None
    assert failure.audit.failure_kind is None
    assert failure.audit.retryability is None
    assert error.provider_error is provider
