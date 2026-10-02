"""Serviceのプロバイダー例外変換を検証する。"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    CLASSIFIED_AI_PROVIDER_ERRORS,
    AIProviderError,
    AIProviderErrorResponseError,
    AIProviderErrorResponseReason,
    AIProviderGenerationError,
    AIProviderGenerationReason,
    AIProviderRequestNotSentError,
    AIProviderRequestNotSentReason,
    AIProviderTransportError,
)
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingFailureReason,
    to_embedding_error,
)
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

# 分類済みの4種類それぞれの代表。
_PROVIDER_ERROR_FACTORIES = [
    pytest.param(
        lambda: AIProviderRequestNotSentError(
            reason=AIProviderRequestNotSentReason.NOT_CONFIGURED
        ),
        id="request_not_sent",
    ),
    pytest.param(
        lambda: AIProviderTransportError(
            transport=HttpTransportFailure(
                HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
            )
        ),
        id="transport",
    ),
    pytest.param(
        lambda: AIProviderErrorResponseError(
            reason=AIProviderErrorResponseReason.RATE_LIMITED, status_code=429
        ),
        id="error_response",
    ),
    pytest.param(
        lambda: AIProviderGenerationError(
            reason=AIProviderGenerationReason.EMBEDDINGS_EMPTY
        ),
        id="generation",
    ),
]


class TestToEmbeddingError:
    """分類済みの provider error の翻訳契約。"""

    @pytest.mark.parametrize("make_error", _PROVIDER_ERROR_FACTORIES)
    def test_preserves_provider_error_identity(self, make_error) -> None:
        original = make_error()

        result = to_embedding_error(original)

        assert result.provider_error is original  # type: ignore[union-attr]

    @pytest.mark.parametrize("make_error", _PROVIDER_ERROR_FACTORIES)
    def test_propagates_code_from_provider_class_var(self, make_error) -> None:
        original = make_error()

        result = to_embedding_error(original)

        assert result.code == original.CODE  # type: ignore[union-attr]

    def test_cases_cover_all_classified_provider_errors(self) -> None:
        covered = {type(case.values[0]()) for case in _PROVIDER_ERROR_FACTORIES}
        assert covered == set(CLASSIFIED_AI_PROVIDER_ERRORS)


class TestToEmbeddingErrorUnregistered:
    """登録されていない ``AIProviderError`` で fail-fast。"""

    def test_bare_provider_error_base_raises_type_error(self) -> None:
        bare = AIProviderError("bare base")

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_embedding_error(bare)

    def test_direct_ai_provider_error_subclass_raises(self) -> None:
        class _UnregisteredProviderError(AIProviderError):
            CODE = "ai_error_unregistered_for_test"

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_embedding_error(_UnregisteredProviderError())


@pytest.mark.parametrize("make_error", _PROVIDER_ERROR_FACTORIES)
def test_provider_error_uses_service_provider_reason(make_error):
    """プロバイダーの障害はServiceの共通理由へ変換する。"""
    error = to_embedding_error(make_error())
    assert isinstance(error, EmbeddingError)
    assert error.reason is EmbeddingFailureReason.PROVIDER_ERROR
