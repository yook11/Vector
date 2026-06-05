"""Stage 3 ACL — ``map_provider_to_curation`` の翻訳契約テスト。

mapper は provider error を「retry / DROP 軸 (Recoverable / TerminalKeep /
TerminalDrop) + 原因軸 (failure_kind = mode 値 / failure_reason = reason 値)」に
翻訳する。Stage 4/5 と異なり 3-way なのは DROP (記事削除) を持つため
(``TARGET_REJECTED`` → TerminalDrop)。leaf → marker / failure_kind の写像は plan の
disposition 表 (spec) を golden として直書きする (provider 自身の ``FAILURE_MODE``
golden は ``test_ai_provider_errors.py`` が所有)。
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
    CurationError,
    CurationRecoverableError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
    map_provider_to_curation,
)
from app.analysis.gemini_error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)

# 代表 reason (mapper は値そのものを failure_reason に運ぶ。種別は不問)。
_CONTENT_REASON = GeminiContentRejectionReason.SAFETY
_STATE_REASON = GeminiStateReason.TIMEOUT

# leaf → (期待 marker, 期待 failure_kind)。plan の disposition 表 (spec) が出所。
# retryable な回復クラス → Recoverable / TARGET_REJECTED → TerminalDrop (記事削除) /
# それ以外 (operator_action_required) → TerminalKeep。
_LEAF_EXPECTATION: dict[type[AIProviderError], tuple[type[CurationError], str]] = {
    AIProviderNetworkError: (CurationRecoverableError, "attempt_scoped"),
    AIProviderServiceUnavailableError: (
        CurationRecoverableError,
        "time_based_recovery",
    ),
    AIProviderRateLimitedError: (CurationRecoverableError, "time_based_recovery"),
    AIProviderUsageLimitExhaustedError: (
        CurationRecoverableError,
        "condition_based_recovery",
    ),
    AIProviderConfigurationError: (
        CurationTerminalKeepError,
        "operator_action_required",
    ),
    AIProviderRequestInvalidError: (
        CurationTerminalKeepError,
        "operator_action_required",
    ),
    AIProviderInsufficientBalanceError: (
        CurationTerminalKeepError,
        "operator_action_required",
    ),
    AIProviderInputRejectedError: (CurationTerminalDropError, "target_rejected"),
    AIProviderOutputBlockedError: (CurationTerminalDropError, "target_rejected"),
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


class TestMapProviderToCuration:
    """全 provider leaf の翻訳契約 (golden 写像)。"""

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_maps_to_expected_marker_and_failure_kind(
        self, exc_type: type[AIProviderError]
    ) -> None:
        expected_marker, expected_kind = _LEAF_EXPECTATION[exc_type]

        result = map_provider_to_curation(_instantiate(exc_type))

        assert isinstance(result, expected_marker)
        assert result.failure_kind == expected_kind  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_preserves_provider_error_identity(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)

        result = map_provider_to_curation(original)

        assert result.provider_error is original  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        result = map_provider_to_curation(_instantiate(exc_type))

        assert result.code == exc_type.CODE  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_carries_reason_value_as_failure_reason(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)
        expected = original.reason.value  # type: ignore[attr-defined]

        result = map_provider_to_curation(original)

        assert result.failure_reason == expected  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "exc_type",
        [t for t in _LEAF_EXPECTATION if not issubclass(t, AIProviderContentError)],
    )
    def test_state_without_reason_has_none_failure_reason(
        self, exc_type: type[AIProviderError]
    ) -> None:
        # state reason は任意。未指定なら failure_reason は焼かれない。
        result = map_provider_to_curation(
            _instantiate(exc_type, with_state_reason=False)
        )

        assert result.failure_reason is None  # type: ignore[union-attr]

    def test_golden_covers_all_provider_leaves(self) -> None:
        # 完備性: provider leaf を増やしたら golden 表も更新する運用 (9 種)。
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


class TestMapProviderToCurationUnregistered:
    """state でも content でもない ``AIProviderError`` で fail-fast。"""

    def test_bare_provider_error_base_raises_type_error(self) -> None:
        bare = AIProviderError("bare base")

        with pytest.raises(TypeError, match="unmapped provider error"):
            map_provider_to_curation(bare)

    def test_direct_ai_provider_error_subclass_raises(self) -> None:
        class _NeitherStateNorContent(AIProviderError):
            CODE = "ai_error_neither_for_test"

        with pytest.raises(TypeError, match="unmapped provider error"):
            map_provider_to_curation(_NeitherStateNorContent())
