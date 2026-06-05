"""Stage 4 ACL — ``map_provider_to_assessment`` の dispatch / 網羅性テスト。"""

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
from app.analysis.assessment.errors import (
    ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS,
    ASSESSMENT_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS,
    ASSESSMENT_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS,
    AssessmentRecoverableError,
    AssessmentTerminalStageBlockedError,
    AssessmentTerminalTargetRejectedError,
    map_provider_to_assessment,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason


def _instantiate(exc_type: type[AIProviderError]) -> AIProviderError:
    """provider error を構築する。

    content 系は ``reason`` 必須なので代表 reason を渡し、state 系は legacy
    positional message を渡す (accept-and-discard 経路を維持する)。mapper は
    reason を読まないため、reason の具体値は dispatch 判定に影響しない。
    """
    if issubclass(exc_type, AIProviderContentError):
        return exc_type(reason=GeminiContentRejectionReason.SAFETY)
    return exc_type("boom")


# 期待 9 種の白リスト。本ファイルで直接 expected セットを書くことで、
# ``AIProviderError.__subclasses__()`` を walking するテスト間副作用を避ける。
# 9 種が増減したらこの値を更新してテストを通す運用。
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
        assert exc_type in ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS

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
        assert exc_type in ASSESSMENT_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS

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
        assert exc_type in ASSESSMENT_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS

    def test_three_tuples_cover_all_expected_provider_error_types(self) -> None:
        # 網羅性: 期待 9 種すべてが 3 tuple のいずれかに登録されている。
        union = (
            frozenset(ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS)
            | frozenset(ASSESSMENT_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS)
            | frozenset(ASSESSMENT_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS)
        )
        assert union == _EXPECTED_PROVIDER_ERROR_TYPES

    def test_three_tuples_are_mutually_exclusive(self) -> None:
        # 排他性: 同じ class が複数 tuple に登録されていない。
        groups = [
            frozenset(ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS),
            frozenset(ASSESSMENT_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS),
            frozenset(ASSESSMENT_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS),
        ]
        for i, left in enumerate(groups):
            for right in groups[i + 1 :]:
                assert left & right == frozenset()

    def test_recoverable_tuple_size(self) -> None:
        assert len(ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS) == 4

    def test_terminal_stage_blocked_tuple_size(self) -> None:
        assert len(ASSESSMENT_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS) == 3

    def test_terminal_target_rejected_tuple_size(self) -> None:
        assert len(ASSESSMENT_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS) == 2


class TestMapProviderToAssessmentRecoverable:
    """Recoverable 系 4 種が ``AssessmentRecoverableError`` に詰め替えられる。"""

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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

        assert isinstance(result, AssessmentRecoverableError)

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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToAssessmentTerminalStageBlocked:
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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

        assert isinstance(result, AssessmentTerminalStageBlockedError)

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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToAssessmentTerminalTargetRejected:
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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

        assert isinstance(result, AssessmentTerminalTargetRejectedError)

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
        original = _instantiate(exc_type)

        result = map_provider_to_assessment(original)

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
        original = _instantiate(exc_type)

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
