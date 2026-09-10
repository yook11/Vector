"""プロバイダー例外のServiceでの原因保持とTaskiq境界での分類を検証する。"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderContentError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderOutputTruncatedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.ai_providers.gemini.error_translator import (
    GeminiContentRejectionReason,
    GeminiStateReason,
)
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentFailureReason,
    AssessmentResponseInvalidError,
    to_assessment_error,
)
from app.analysis.assessment.task_errors import (
    AssessmentRecoverableError,
    AssessmentTaskError,
    AssessmentTerminalError,
    to_assessment_task_error,
)
from app.db.errors import DatabaseConnectionError, DatabaseConnectionErrorReason

# 代表 reason (mapper は値そのものを failure_reason に運ぶ。種別は不問)。
_CONTENT_REASON = GeminiContentRejectionReason.SAFETY
_STATE_REASON = GeminiStateReason.TIMEOUT

# leaf → (期待 marker, 期待 failure_kind)。plan の disposition 表 (spec) が出所。
# retryable な回復クラス (attempt_scoped / time_based / condition_based) は
# Recoverable、非 retryable (operator_action / target_rejected) は Terminal。
_LEAF_EXPECTATION: dict[
    type[AIProviderError], tuple[type[AssessmentTaskError], str]
] = {
    AIProviderNetworkError: (AssessmentRecoverableError, "attempt_scoped"),
    AIProviderOutputTruncatedError: (AssessmentRecoverableError, "attempt_scoped"),
    AIProviderServiceUnavailableError: (
        AssessmentRecoverableError,
        "time_based_recovery",
    ),
    AIProviderRateLimitedError: (AssessmentRecoverableError, "time_based_recovery"),
    AIProviderUsageLimitExhaustedError: (
        AssessmentRecoverableError,
        "condition_based_recovery",
    ),
    AIProviderConfigurationError: (AssessmentTerminalError, "operator_action_required"),
    AIProviderRequestInvalidError: (
        AssessmentTerminalError,
        "operator_action_required",
    ),
    AIProviderInsufficientBalanceError: (
        AssessmentTerminalError,
        "operator_action_required",
    ),
    AIProviderInputRejectedError: (AssessmentTerminalError, "target_rejected"),
    AIProviderOutputBlockedError: (AssessmentTerminalError, "target_rejected"),
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


class TestProviderToAssessmentTaskError:
    """全 provider leaf の翻訳契約 (golden 写像)。"""

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_maps_to_expected_marker_and_failure_kind(
        self, exc_type: type[AIProviderError]
    ) -> None:
        expected_marker, expected_kind = _LEAF_EXPECTATION[exc_type]

        result = to_assessment_task_error(to_assessment_error(_instantiate(exc_type)))

        assert isinstance(result, expected_marker)
        assert result.failure_kind == expected_kind

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_preserves_provider_error_identity(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)

        result = to_assessment_task_error(to_assessment_error(original))

        assert result.provider_error is original  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_propagates_code_from_provider_class_var(
        self, exc_type: type[AIProviderError]
    ) -> None:
        result = to_assessment_task_error(to_assessment_error(_instantiate(exc_type)))

        assert result.code == exc_type.CODE  # type: ignore[union-attr]

    @pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
    def test_carries_reason_value_as_failure_reason(
        self, exc_type: type[AIProviderError]
    ) -> None:
        original = _instantiate(exc_type)
        expected = original.reason.value  # type: ignore[attr-defined]

        result = to_assessment_task_error(to_assessment_error(original))

        assert result.failure_reason == expected  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "exc_type",
        [t for t in _LEAF_EXPECTATION if not issubclass(t, AIProviderContentError)],
    )
    def test_state_without_reason_has_none_failure_reason(
        self, exc_type: type[AIProviderError]
    ) -> None:
        # state reason は任意。未指定なら failure_reason は焼かれない。
        result = to_assessment_task_error(
            to_assessment_error(_instantiate(exc_type, with_state_reason=False))
        )

        assert result.failure_reason is None  # type: ignore[union-attr]

    def test_golden_covers_all_provider_leaves(self) -> None:
        # 完備性: provider leaf を増やしたら golden 表も更新する運用 (10 種)。
        expected = frozenset(
            {
                AIProviderConfigurationError,
                AIProviderRequestInvalidError,
                AIProviderInsufficientBalanceError,
                AIProviderRateLimitedError,
                AIProviderUsageLimitExhaustedError,
                AIProviderServiceUnavailableError,
                AIProviderNetworkError,
                AIProviderOutputTruncatedError,
                AIProviderInputRejectedError,
                AIProviderOutputBlockedError,
            }
        )
        assert frozenset(_LEAF_EXPECTATION) == expected


class TestMapProviderToAssessmentUnregistered:
    """state でも content でもない ``AIProviderError`` で fail-fast。"""

    def test_bare_provider_error_base_raises_type_error(self) -> None:
        bare = AIProviderError("bare base")

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_assessment_task_error(to_assessment_error(bare))

    def test_direct_ai_provider_error_subclass_raises(self) -> None:
        class _NeitherStateNorContent(AIProviderError):
            CODE = "ai_error_neither_for_test"

        with pytest.raises(TypeError, match="unmapped provider error"):
            to_assessment_task_error(to_assessment_error(_NeitherStateNorContent()))


@pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
def test_service_contract_retains_provider_details_without_retry_classification(
    exc_type,
):
    original = _instantiate(exc_type)
    result = to_assessment_error(original)
    assert type(result) is AssessmentError
    assert result.reason is AssessmentFailureReason.PROVIDER_ERROR
    assert result.provider_error is original
    assert result.provider_error.reason is original.reason
    assert result.code == original.CODE
    assert result.defect is None
    assert not hasattr(result, "RETRYABILITY")
    task_error = to_assessment_task_error(result)
    assert task_error.__cause__ is result
    assert task_error.provider_error is original


@pytest.mark.parametrize("exc_type", list(_LEAF_EXPECTATION))
@pytest.mark.asyncio
async def test_service_wraps_provider_error_before_opening_db(exc_type):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.analysis.assessment.domain.ready import ReadyForAssessment
    from app.analysis.assessment.service import AssessmentService

    def no_session():
        raise AssertionError("AI失敗時はDBを開かない")

    original = _instantiate(exc_type)
    assessor = SimpleNamespace(assess=AsyncMock(side_effect=original))
    service = AssessmentService(no_session)
    ready = ReadyForAssessment(
        curation_id=1, translated_title="title", summary="summary"
    )
    with pytest.raises(AssessmentError) as raised:
        await service.execute(ready, assessor, analyzable_article_id=1)
    assert raised.value.reason is AssessmentFailureReason.PROVIDER_ERROR
    assert raised.value.provider_error is original
    assert raised.value.__cause__ is original
    assert raised.value.code == original.CODE


@pytest.mark.parametrize(
    "original",
    [
        AssessmentResponseInvalidError(AssessmentResponseDefect.CATEGORY_KEY_MISSING),
        DatabaseConnectionError(reason=DatabaseConnectionErrorReason.CONNECTION_FAILED),
        TimeoutError("timeout"),
        RuntimeError("unexpected"),
    ],
)
@pytest.mark.asyncio
async def test_service_preserves_non_provider_exception_identity(original):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.analysis.assessment.domain.ready import ReadyForAssessment
    from app.analysis.assessment.service import AssessmentService

    def no_session():
        raise AssertionError("AI失敗時はDBを開かない")

    ready = ReadyForAssessment(
        curation_id=1, translated_title="title", summary="summary"
    )
    assessor = SimpleNamespace(assess=AsyncMock(side_effect=original))
    with pytest.raises(type(original)) as raised:
        await AssessmentService(no_session).execute(
            ready, assessor, analyzable_article_id=1
        )
    assert raised.value is original
    assert original.__cause__ is None
