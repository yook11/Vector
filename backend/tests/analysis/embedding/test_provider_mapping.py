"""Stage 5 ACL — ``to_embedding_error`` の dispatch / 網羅性テスト。"""

from __future__ import annotations

import pytest

from app.analysis.embedding.errors import (
    EMBEDDING_RECOVERABLE_PROVIDER_ERRORS,
    EMBEDDING_TERMINAL_SKIP_PROVIDER_ERRORS,
    EmbeddingRecoverableError,
    EmbeddingTerminalSkipError,
    to_embedding_error,
)
from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)

# 期待 9 種の白リスト。Stage 4 test_provider_mapping.py と同形。
_EXPECTED_PROVIDER_ERROR_TYPES: frozenset[type[AIProviderError]] = frozenset(
    {
        AIProviderConfigurationError,
        AIProviderRequestInvalidError,
        AIProviderInsufficientBalanceError,
        AIProviderRateLimitedError,
        AIProviderQuotaExhaustedError,
        AIProviderServiceUnavailableError,
        AIProviderNetworkError,
        AIProviderInputRejectedError,
        AIProviderOutputBlockedError,
    }
)


class TestTupleContents:
    """tuple に登録されている class の固定値 / 網羅性 / 排他性。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderNetworkError,
            AIProviderServiceUnavailableError,
            AIProviderRateLimitedError,
            AIProviderQuotaExhaustedError,
        ],
    )
    def test_recoverable_tuple_contains_expected_types(
        self, exc_type: type[AIProviderError]
    ) -> None:
        assert exc_type in EMBEDDING_RECOVERABLE_PROVIDER_ERRORS

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_terminal_skip_tuple_contains_expected_types(
        self, exc_type: type[AIProviderError]
    ) -> None:
        assert exc_type in EMBEDDING_TERMINAL_SKIP_PROVIDER_ERRORS

    def test_two_tuples_cover_all_expected_provider_error_types(self) -> None:
        # 網羅性: 期待 9 種すべてが 2 tuple のいずれかに登録されている。
        union = frozenset(EMBEDDING_RECOVERABLE_PROVIDER_ERRORS) | frozenset(
            EMBEDDING_TERMINAL_SKIP_PROVIDER_ERRORS
        )
        assert union == _EXPECTED_PROVIDER_ERROR_TYPES

    def test_two_tuples_are_mutually_exclusive(self) -> None:
        # 排他性: 同じ class が両 tuple に登録されていない。
        intersection = frozenset(EMBEDDING_RECOVERABLE_PROVIDER_ERRORS) & frozenset(
            EMBEDDING_TERMINAL_SKIP_PROVIDER_ERRORS
        )
        assert intersection == frozenset()

    def test_recoverable_tuple_size(self) -> None:
        assert len(EMBEDDING_RECOVERABLE_PROVIDER_ERRORS) == 4

    def test_terminal_skip_tuple_size(self) -> None:
        assert len(EMBEDDING_TERMINAL_SKIP_PROVIDER_ERRORS) == 5


class TestMapProviderToEmbeddingRecoverable:
    """Recoverable 系 4 種が ``EmbeddingRecoverableError`` に詰め替えられる。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderNetworkError,
            AIProviderServiceUnavailableError,
            AIProviderRateLimitedError,
            AIProviderQuotaExhaustedError,
        ],
    )
    def test_dispatches_to_recoverable_marker(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert isinstance(result, EmbeddingRecoverableError)

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderNetworkError,
            AIProviderServiceUnavailableError,
            AIProviderRateLimitedError,
            AIProviderQuotaExhaustedError,
        ],
    )
    def test_preserves_provider_error_identity(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert result.provider_error is original  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderNetworkError,
            AIProviderServiceUnavailableError,
            AIProviderRateLimitedError,
            AIProviderQuotaExhaustedError,
        ],
    )
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToEmbeddingTerminalSkip:
    """TerminalSkip 系 5 種が ``EmbeddingTerminalSkipError`` に詰め替えられる。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_dispatches_to_terminal_skip_marker(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert isinstance(result, EmbeddingTerminalSkipError)

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_preserves_provider_error_identity(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert result.provider_error is original  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToEmbeddingUnregistered:
    """tuple に未登録の ``AIProviderError`` subclass で fail-fast。"""

    def test_unregistered_subclass_raises_type_error(self) -> None:
        class _UnregisteredProviderError(AIProviderError):
            """テスト内 ad-hoc subclass。tuple に登録されていない。"""

            CODE = "ai_error_unregistered_for_test"

        unregistered = _UnregisteredProviderError("brand-new failure mode")

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_embedding_error(unregistered)

    def test_bare_provider_error_base_raises_type_error(self) -> None:
        # 基底 ``AIProviderError`` 自身も tuple に未登録なので TypeError。
        bare = AIProviderError("bare base")

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_embedding_error(bare)
