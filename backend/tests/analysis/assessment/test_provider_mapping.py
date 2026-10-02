"""プロバイダー例外のServiceでの原因保持を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ai_providers.errors import (
    CLASSIFIED_AI_PROVIDER_ERRORS,
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
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
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# 分類済みの4種類それぞれの代表。
_PROVIDER_ERROR_FACTORIES = [
    pytest.param(
        lambda: AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED),
        id="request_not_sent",
    ),
    pytest.param(
        lambda: AIProviderTransportError(
            http_error=HttpTransportError(
                failure=HttpTransportFailure(
                    HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                )
            )
        ),
        id="transport",
    ),
    pytest.param(
        lambda: AIProviderResponseError(
            reason=AIProviderResponseReason.INSUFFICIENT_BALANCE,
            http_error=HttpResponseError(status_code=402, received_at=_RECEIVED_AT),
        ),
        id="error_response",
    ),
    pytest.param(
        lambda: AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED),
        id="generation",
    ),
]


def test_cases_cover_all_classified_provider_errors() -> None:
    covered = {type(case.values[0]()) for case in _PROVIDER_ERROR_FACTORIES}
    assert covered == set(CLASSIFIED_AI_PROVIDER_ERRORS)


@pytest.fixture
def assessment_logger():
    return create_article_analysis_logger().bind(stage="assessment")


@pytest.mark.parametrize("make_error", _PROVIDER_ERROR_FACTORIES)
def test_service_contract_retains_provider_details_without_retry_classification(
    make_error,
):
    original = make_error()
    result = to_assessment_error(original)
    assert type(result) is AssessmentError
    assert result.reason is AssessmentFailureReason.PROVIDER_ERROR
    assert result.provider_error is original
    assert result.provider_error.reason is original.reason
    assert result.code == original.CODE
    assert result.defect is None
    assert not hasattr(result, "RETRYABILITY")


@pytest.mark.parametrize("make_error", _PROVIDER_ERROR_FACTORIES)
@pytest.mark.asyncio
async def test_service_wraps_provider_error_before_opening_db(
    make_error, assessment_logger
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.analysis.assessment.domain.ready import ReadyForAssessment
    from app.analysis.assessment.service import AssessmentService

    def no_session():
        raise AssertionError("AI失敗時はDBを開かない")

    original = make_error()
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
