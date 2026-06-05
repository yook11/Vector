"""Stage 5 ACL — ``to_embedding_error`` の翻訳契約テスト (Stage 4 と同形)。

mapper は provider error を「retry 軸 (Recoverable / Terminal) + 原因軸
(failure_kind = mode 値 / failure_reason = reason 値)」に翻訳する。leaf → marker /
failure_kind の写像は plan の disposition 表 (spec) を golden として直書きする。
"""

from __future__ import annotations

import pytest

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderContentError,
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
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingRecoverableError,
    EmbeddingTerminalError,
    to_embedding_error,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)

_CONTENT_REASON = GeminiContentRejectionReason.SAFETY
_STATE_REASON = GeminiStateReason.TIMEOUT

# leaf → (期待 marker, 期待 failure_kind)。plan の disposition 表 (spec) が出所。
_LEAF_EXPECTATION: dict[type[AIProviderError], tuple[type[EmbeddingError], str]] = {
    AIProviderNetworkError: (EmbeddingRecoverableError, "attempt_scoped"),
    AIProviderServiceUnavailableError: (
        EmbeddingRecoverableError,
        "time_based_recovery",
    ),
    AIProviderRateLimitedError: (EmbeddingRecoverableError, "time_based_recovery"),
    AIProviderUsageLimitExhaustedError: (
        EmbeddingRecoverableError,
        "condition_based_recovery",
    ),
    AIProviderConfigurationError: (EmbeddingTerminalError, "operator_action_required"),
    AIProviderRequestInvalidError: (EmbeddingTerminalError, "operator_action_required"),
    AIProviderInsufficientBalanceError: (
        EmbeddingTerminalError,
        "operator_action_required",
    ),
    AIProviderInputRejectedError: (EmbeddingTerminalError, "target_rejected"),
    AIProviderOutputBlockedError: (EmbeddingTerminalError, "target_rejected"),
}


def _instantiate(
    exc_type: type[AIProviderError], *, with_state_reason: bool = True
) -> AIProviderError:
    """provider error を構築する。content 系は reason 必須、state 系は任意。"""
    if issubclass(exc_type, AIProviderContentError):
        return exc_type(reason=_CONTENT_REASON)
    if with_state_reason:
        return exc_type(reason=_STATE_REASON)
    return exc_type()


class TestToEmbeddingError:
    """全 provider leaf の翻訳契約 (golden 写像)。"""

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_maps_to_expected_marker_and_failure_kind(
        self, exc_type: type[AIProviderError]
    ) -> None:
        expected_marker, expected_kind = _LEAF_EXPECTATION[exc_type]

        result = to_embedding_error(_instantiate(exc_type))

        assert isinstance(result, expected_marker)
        assert result.failure_kind == expected_kind

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_preserves_provider_error_identity(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)

        result = to_embedding_error(original)

        assert result.provider_error is original  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        result = to_embedding_error(_instantiate(exc_type))

        assert result.code == exc_type.CODE  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_carries_reason_value_as_failure_reason(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)
        expected = original.reason.value  # type: ignore[attr-defined]

        result = to_embedding_error(original)

        assert result.failure_reason == expected  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "exc_type",
        [t for t in _LEAF_EXPECTATION if not issubclass(t, AIProviderContentError)],
    )
    def test_state_without_reason_has_none_failure_reason(
        self, exc_type: type[AIProviderError]
    ) -> None:
        result = to_embedding_error(_instantiate(exc_type, with_state_reason=False))

        assert result.failure_reason is None  # type: ignore[union-attr]

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
        assert frozenset(_LEAF_EXPECTATION) == expected


class TestToEmbeddingErrorUnregistered:
    """state でも content でもない ``AIProviderError`` で fail-fast。"""

    def test_bare_provider_error_base_raises_type_error(self) -> None:
        bare = AIProviderError("bare base")

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_embedding_error(bare)

    def test_direct_ai_provider_error_subclass_raises(self) -> None:
        class _NeitherStateNorContent(AIProviderError):
            CODE = "ai_error_neither_for_test"

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_embedding_error(_NeitherStateNorContent())
