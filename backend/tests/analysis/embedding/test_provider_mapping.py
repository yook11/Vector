"""Stage 5 ACL — ``to_embedding_error`` の dispatch / 網羅性テスト。"""

from __future__ import annotations

import pytest

from app.analysis.ai_provider_errors import (
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
from app.analysis.embedding.errors import (
    EMBEDDING_RECOVERABLE_PROVIDER_ERRORS,
    EMBEDDING_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS,
    EMBEDDING_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS,
    EmbeddingRecoverableError,
    EmbeddingTerminalStageBlockedError,
    EmbeddingTerminalTargetRejectedError,
    to_embedding_error,
)

# 期待 9 種の白リスト。Stage 4 test_provider_mapping.py と同形。
_EXPECTED_PROVIDER_ERROR_TYPES: frozenset[type[AIProviderError]] = frozenset(
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


class TestTupleContents:
    """tuple に登録されている class の固定値 / 網羅性 / 排他性。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderNetworkError,
            AIProviderServiceUnavailableError,
            AIProviderRateLimitedError,
            AIProviderUsageLimitExhaustedError,
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
        ],
    )
    def test_terminal_stage_blocked_tuple_contains_expected_types(
        self, exc_type: type[AIProviderError]
    ) -> None:
        assert exc_type in EMBEDDING_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_terminal_target_rejected_tuple_contains_expected_types(
        self, exc_type: type[AIProviderError]
    ) -> None:
        assert exc_type in EMBEDDING_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS

    def test_three_tuples_cover_all_expected_provider_error_types(self) -> None:
        # 網羅性: 期待 9 種すべてが 3 tuple のいずれかに登録されている。
        union = (
            frozenset(EMBEDDING_RECOVERABLE_PROVIDER_ERRORS)
            | frozenset(EMBEDDING_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS)
            | frozenset(EMBEDDING_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS)
        )
        assert union == _EXPECTED_PROVIDER_ERROR_TYPES

    def test_three_tuples_are_mutually_exclusive(self) -> None:
        # 排他性: 同じ class が複数 tuple に登録されていない。
        groups = [
            frozenset(EMBEDDING_RECOVERABLE_PROVIDER_ERRORS),
            frozenset(EMBEDDING_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS),
            frozenset(EMBEDDING_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS),
        ]
        for i, left in enumerate(groups):
            for right in groups[i + 1 :]:
                assert left & right == frozenset()

    def test_recoverable_tuple_size(self) -> None:
        assert len(EMBEDDING_RECOVERABLE_PROVIDER_ERRORS) == 4

    def test_terminal_stage_blocked_tuple_size(self) -> None:
        assert len(EMBEDDING_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS) == 3

    def test_terminal_target_rejected_tuple_size(self) -> None:
        assert len(EMBEDDING_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS) == 2


class TestMapProviderToEmbeddingRecoverable:
    """Recoverable 系 4 種が ``EmbeddingRecoverableError`` に詰め替えられる。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderNetworkError,
            AIProviderServiceUnavailableError,
            AIProviderRateLimitedError,
            AIProviderUsageLimitExhaustedError,
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
            AIProviderUsageLimitExhaustedError,
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
            AIProviderUsageLimitExhaustedError,
        ],
    )
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToEmbeddingTerminalStageBlocked:
    """stage-wide terminal 系 3 種が StageBlocked marker に詰め替えられる。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
        ],
    )
    def test_dispatches_to_terminal_stage_blocked_marker(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert isinstance(result, EmbeddingTerminalStageBlockedError)

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
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
        ],
    )
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToEmbeddingTerminalTargetRejected:
    """target-local terminal 系 2 種が TargetRejected marker に詰め替えられる。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_dispatches_to_terminal_target_rejected_marker(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = exc_type("boom")

        result = to_embedding_error(original)

        assert isinstance(result, EmbeddingTerminalTargetRejectedError)

    @pytest.mark.parametrize(
        "exc_type",
        [
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
