"""Stage 3 ACL — ``map_provider_to_curation`` の dispatch / 網羅性テスト。

Stage 3 は article DELETE / Keep / Recoverable の 3 軸を持つため tuple も 3 つ。
Stage 4 / Stage 5 (2 tuple) と構造は同じ。
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
from app.analysis.curation.errors import (
    CURATION_RECOVERABLE_PROVIDER_ERRORS,
    CURATION_TERMINAL_DROP_PROVIDER_ERRORS,
    CURATION_TERMINAL_KEEP_PROVIDER_ERRORS,
    CurationRecoverableError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
    map_provider_to_curation,
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
        assert exc_type in CURATION_RECOVERABLE_PROVIDER_ERRORS

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
        ],
    )
    def test_terminal_keep_tuple_contains_expected_types(
        self, exc_type: type[AIProviderError]
    ) -> None:
        assert exc_type in CURATION_TERMINAL_KEEP_PROVIDER_ERRORS

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_terminal_drop_tuple_contains_expected_types(
        self, exc_type: type[AIProviderError]
    ) -> None:
        assert exc_type in CURATION_TERMINAL_DROP_PROVIDER_ERRORS

    def test_three_tuples_cover_all_expected_provider_error_types(self) -> None:
        # 網羅性: 期待 9 種すべてが 3 tuple のいずれかに登録されている。
        union = (
            frozenset(CURATION_RECOVERABLE_PROVIDER_ERRORS)
            | frozenset(CURATION_TERMINAL_KEEP_PROVIDER_ERRORS)
            | frozenset(CURATION_TERMINAL_DROP_PROVIDER_ERRORS)
        )
        assert union == _EXPECTED_PROVIDER_ERROR_TYPES

    def test_three_tuples_are_mutually_exclusive(self) -> None:
        # 排他性: 同じ class が複数 tuple に登録されていない。
        recoverable = frozenset(CURATION_RECOVERABLE_PROVIDER_ERRORS)
        terminal_keep = frozenset(CURATION_TERMINAL_KEEP_PROVIDER_ERRORS)
        terminal_drop = frozenset(CURATION_TERMINAL_DROP_PROVIDER_ERRORS)
        assert recoverable & terminal_keep == frozenset()
        assert recoverable & terminal_drop == frozenset()
        assert terminal_keep & terminal_drop == frozenset()

    def test_recoverable_tuple_size(self) -> None:
        assert len(CURATION_RECOVERABLE_PROVIDER_ERRORS) == 4

    def test_terminal_keep_tuple_size(self) -> None:
        assert len(CURATION_TERMINAL_KEEP_PROVIDER_ERRORS) == 3

    def test_terminal_drop_tuple_size(self) -> None:
        assert len(CURATION_TERMINAL_DROP_PROVIDER_ERRORS) == 2


class TestMapProviderToExtractionRecoverable:
    """Recoverable 系 4 種が ``CurationRecoverableError`` に詰め替えられる。"""

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

        result = map_provider_to_curation(original)

        assert isinstance(result, CurationRecoverableError)

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

        result = map_provider_to_curation(original)

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

        result = map_provider_to_curation(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToExtractionTerminalKeep:
    """TerminalKeep 系 3 種が ``CurationTerminalKeepError`` に詰め替えられる。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderConfigurationError,
            AIProviderRequestInvalidError,
            AIProviderInsufficientBalanceError,
        ],
    )
    def test_dispatches_to_terminal_keep_marker(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)

        result = map_provider_to_curation(original)

        assert isinstance(result, CurationTerminalKeepError)

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

        result = map_provider_to_curation(original)

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

        result = map_provider_to_curation(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToExtractionTerminalDrop:
    """TerminalDrop 系 2 種が ``CurationTerminalDropError`` に詰め替えられる。"""

    @pytest.mark.parametrize(
        "exc_type",
        [
            AIProviderInputRejectedError,
            AIProviderOutputBlockedError,
        ],
    )
    def test_dispatches_to_terminal_drop_marker(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)

        result = map_provider_to_curation(original)

        assert isinstance(result, CurationTerminalDropError)

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

        result = map_provider_to_curation(original)

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

        result = map_provider_to_curation(original)

        assert result.code == exc_type.CODE  # type: ignore[union-attr]


class TestMapProviderToExtractionUnregistered:
    """tuple に未登録の ``AIProviderError`` subclass で fail-fast。"""

    def test_unregistered_subclass_raises_type_error(self) -> None:
        class _UnregisteredProviderError(AIProviderError):
            """テスト内 ad-hoc subclass。tuple に登録されていない。"""

            CODE = "ai_error_unregistered_for_test"

        unregistered = _UnregisteredProviderError("brand-new failure mode")

        with pytest.raises(TypeError, match="unmapped provider error"):
            map_provider_to_curation(unregistered)

    def test_bare_provider_error_base_raises_type_error(self) -> None:
        # 基底 ``AIProviderError`` 自身も tuple に未登録なので TypeError。
        bare = AIProviderError("bare base")

        with pytest.raises(TypeError, match="unmapped provider error"):
            map_provider_to_curation(bare)
