"""Stage 4 ACL — ``map_provider_to_assessment`` の dispatch / 網羅性テスト。"""

from __future__ import annotations

import pytest

from app.analysis.assessment.errors import (
    ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS,
    ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS,
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
    map_provider_to_assessment,
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

# 期待 9 種の白リスト。本ファイルで直接 expected セットを書くことで、
# ``AIProviderError.__subclasses__()`` を walking するテスト間副作用を避ける。
# 9 種が増減したらこの値を更新してテストを通す運用。
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
        assert exc_type in ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS

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
        assert exc_type in ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS

    def test_two_tuples_cover_all_expected_provider_error_types(self) -> None:
        # 網羅性: 期待 9 種すべてが 2 tuple のいずれかに登録されている。
        union = frozenset(ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS) | frozenset(
            ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS
        )
        assert union == _EXPECTED_PROVIDER_ERROR_TYPES

    def test_two_tuples_are_mutually_exclusive(self) -> None:
        # 排他性: 同じ class が両 tuple に登録されていない。
        intersection = frozenset(ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS) & frozenset(
            ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS
        )
        assert intersection == frozenset()

    def test_recoverable_tuple_size(self) -> None:
        assert len(ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS) == 4

    def test_terminal_skip_tuple_size(self) -> None:
        assert len(ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS) == 5


class TestMapProviderToAssessmentRecoverable:
    """Recoverable 系 4 種が ``AssessmentRecoverableError`` に詰め替えられる。"""

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

        result = map_provider_to_assessment(original)

        assert isinstance(result, AssessmentRecoverableError)

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

        result = map_provider_to_assessment(original)

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

        result = map_provider_to_assessment(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToAssessmentTerminalSkip:
    """TerminalSkip 系 5 種が ``AssessmentTerminalSkipError`` に詰め替えられる。"""

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

        result = map_provider_to_assessment(original)

        assert isinstance(result, AssessmentTerminalSkipError)

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

        result = map_provider_to_assessment(original)

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

        result = map_provider_to_assessment(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToAssessmentUnregistered:
    """tuple に未登録の ``AIProviderError`` subclass で fail-fast。"""

    def test_unregistered_subclass_raises_type_error(self) -> None:
        class _UnregisteredProviderError(AIProviderError):
            """テスト内 ad-hoc subclass。tuple に登録されていない。"""

            CODE = "ai_error_unregistered_for_test"

        unregistered = _UnregisteredProviderError("brand-new failure mode")

        with pytest.raises(TypeError, match="unmapped provider error"):
            map_provider_to_assessment(unregistered)

    def test_bare_provider_error_base_raises_type_error(self) -> None:
        # 基底 ``AIProviderError`` 自身も tuple に未登録なので TypeError。
        bare = AIProviderError("bare base")

        with pytest.raises(TypeError, match="unmapped provider error"):
            map_provider_to_assessment(bare)
