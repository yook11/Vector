"""Embedding Serviceの失敗理由の契約。"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderRateLimitedError,
)
from app.analysis.embedding.errors import (
    EmbeddingAnalyzedArticleMissingError,
    EmbeddingError,
    EmbeddingFailureReason,
    EmbeddingResponseInvalidError,
)


class TestEmbeddingResponseInvalidError:
    """応答不正はServiceの失敗理由であり、再試行分類を継承しない。"""

    def test_is_service_error_subclass(self) -> None:
        assert issubclass(EmbeddingResponseInvalidError, EmbeddingError)

    def test_holds_fixed_code(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.code == "embedding_response_invalid"

    def test_reason_is_response_invalid(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.reason is EmbeddingFailureReason.RESPONSE_INVALID

    def test_has_no_provider_error_or_retry_policy(self) -> None:
        exc = EmbeddingResponseInvalidError()
        assert exc.provider_error is None
        assert not hasattr(exc, "RETRYABILITY")

    def test_str_renders_code_only(self) -> None:
        exc = EmbeddingResponseInvalidError()
        expected = "EmbeddingResponseInvalidError(code='embedding_response_invalid')"
        assert str(exc) == expected

    def test_positional_message_rejected(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingResponseInvalidError("dimension mismatch")  # type: ignore[call-arg]


@pytest.mark.parametrize("reason", ["article_missing", None])
def test_failure_reason_rejects_untyped_values(reason):
    """自由文字列を失敗理由として受け付けない。"""
    with pytest.raises(TypeError):
        EmbeddingError(reason=reason)


def test_provider_reason_requires_classified_provider_error():
    """プロバイダー障害には詳細を持つ元の例外を必須とする。"""
    with pytest.raises(TypeError):
        EmbeddingError(reason=EmbeddingFailureReason.PROVIDER_ERROR)
    with pytest.raises(TypeError):
        EmbeddingError(
            reason=EmbeddingFailureReason.ARTICLE_MISSING,
            provider_error=AIProviderRateLimitedError(),
        )


@pytest.mark.parametrize(
    ("error_type", "reason"),
    [
        (EmbeddingAnalyzedArticleMissingError, EmbeddingFailureReason.ARTICLE_MISSING),
        (EmbeddingResponseInvalidError, EmbeddingFailureReason.RESPONSE_INVALID),
    ],
)
def test_service_error_carries_reason_without_retry_policy(error_type, reason):
    """Serviceの失敗理由には配送側の再試行方針を持ち込まない。"""
    error = error_type()
    assert error.reason is reason
    assert not hasattr(error, "RETRYABILITY")
