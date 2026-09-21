"""Serviceのプロバイダー例外変換を検証する。"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.ai_providers.gemini.error_translator import (
    GeminiStateReason,
)
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingFailureReason,
    to_embedding_error,
)

_STATE_REASON = GeminiStateReason.TIMEOUT

_PROVIDER_LEAVES = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


def _instantiate(exc_type: type[AIProviderError]) -> AIProviderError:
    """変換先でも保持される任意の理由を付与する。"""
    return exc_type(reason=_STATE_REASON)


class TestToEmbeddingError:
    """全 provider leaf の翻訳契約 (golden 写像)。"""

    @pytest.mark.parametrize("exc_type", list(_PROVIDER_LEAVES))
    def test_preserves_provider_error_identity(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)

        result = to_embedding_error(original)

        assert result.provider_error is original  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_PROVIDER_LEAVES))
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        result = to_embedding_error(_instantiate(exc_type))

        assert result.code == exc_type.CODE  # type: ignore[union-attr]

    def test_golden_covers_all_provider_leaves(self) -> None:
        expected = frozenset(
            {
                AIProviderConfigurationError,
                AIProviderRequestInvalidError,
                AIProviderInsufficientBalanceError,
                AIProviderRateLimitedError,
                AIProviderUsageLimitExhaustedError,
                AIProviderServiceUnavailableError,
                AIProviderNetworkError,
                AIProviderInputRejectedError,
                AIProviderOutputBlockedError,
            }
        )
        assert frozenset(_PROVIDER_LEAVES) == expected


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


@pytest.mark.parametrize("exc_type", _PROVIDER_LEAVES)
def test_provider_error_uses_service_provider_reason(exc_type):
    """プロバイダーの障害はServiceの共通理由へ変換する。"""
    error = to_embedding_error(_instantiate(exc_type))
    assert isinstance(error, EmbeddingError)
    assert error.reason is EmbeddingFailureReason.PROVIDER_ERROR
