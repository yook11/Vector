"""プロバイダー例外のServiceでの原因保持を検証する。"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderConfigurationError,
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
    GeminiStateReason,
)
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentFailureReason,
    AssessmentResponseInvalidError,
    to_assessment_error,
)
from app.analysis.logging import create_article_analysis_logger
from app.db.errors import DatabaseConnectionError, DatabaseConnectionErrorReason

# 代表 reason (mapper は値そのものを failure_reason に運ぶ。種別は不問)。
_STATE_REASON = GeminiStateReason.TIMEOUT

_PROVIDER_ERRORS = (
    AIProviderNetworkError,
    AIProviderOutputTruncatedError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


@pytest.fixture
def assessment_logger():
    return create_article_analysis_logger().bind(stage="assessment")


def _instantiate(exc_type: type[AIProviderError]) -> AIProviderError:
    """変換先でも保持される任意の理由を付与する。"""
    return exc_type(reason=_STATE_REASON)


@pytest.mark.parametrize("exc_type", _PROVIDER_ERRORS)
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


@pytest.mark.parametrize("exc_type", _PROVIDER_ERRORS)
@pytest.mark.asyncio
async def test_service_wraps_provider_error_before_opening_db(
    exc_type, assessment_logger
):
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
        await service.execute(
            ready, assessor, analyzable_article_id=1, logger=assessment_logger
        )
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
async def test_service_preserves_non_provider_exception_identity(
    original, assessment_logger
):
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
            ready, assessor, analyzable_article_id=1, logger=assessment_logger
        )
    assert raised.value is original
    assert original.__cause__ is None


@pytest.mark.asyncio
async def test_service_passes_message_logger_to_assessor(assessment_logger):
    """Serviceが受け取ったロガーをそのままAssessorへ渡す。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from app.analysis.assessment.domain.ready import ReadyForAssessment
    from app.analysis.assessment.service import AssessmentService

    failure = RuntimeError("stop before database")
    assessor = SimpleNamespace(assess=AsyncMock(side_effect=failure))
    ready = ReadyForAssessment(
        curation_id=1, translated_title="title", summary="summary"
    )
    with pytest.raises(RuntimeError):
        await AssessmentService(Mock()).execute(
            ready, assessor, analyzable_article_id=1, logger=assessment_logger
        )
    assessor.assess.assert_awaited_once_with(
        title_ja="title", summary_ja="summary", logger=assessment_logger
    )
