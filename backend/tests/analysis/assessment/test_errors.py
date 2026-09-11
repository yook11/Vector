"""Assessmentの失敗理由・詳細の組み合わせと観測コードを検証する。"""

from enum import StrEnum

import pytest

from app.ai_providers.errors import AIProviderError, AIProviderNetworkError
from app.analysis.assessment.errors import (
    AssessmentCurationMissingError,
    AssessmentError,
    AssessmentFailureReason,
    AssessmentResponseInvalidError,
    to_assessment_error,
)
from app.analysis.assessment.task_errors import AssessmentTaskError


class SampleDefect(StrEnum):
    INVALID = "assessment_test_invalid"


@pytest.mark.parametrize(
    "reason", ["provider_error", "response_invalid", "curation_missing", None, 1]
)
def test_requires_reason_enum(reason):
    with pytest.raises(TypeError):
        AssessmentError(reason=reason)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reason": AssessmentFailureReason.PROVIDER_ERROR},
        {
            "reason": AssessmentFailureReason.PROVIDER_ERROR,
            "provider_error": AIProviderError(),
        },
        {
            "reason": AssessmentFailureReason.PROVIDER_ERROR,
            "provider_error": ValueError("private"),
        },
        {
            "reason": AssessmentFailureReason.PROVIDER_ERROR,
            "provider_error": AIProviderNetworkError(),
            "defect": SampleDefect.INVALID,
        },
        {"reason": AssessmentFailureReason.RESPONSE_INVALID},
        {
            "reason": AssessmentFailureReason.RESPONSE_INVALID,
            "defect": "private response",
        },
        {
            "reason": AssessmentFailureReason.RESPONSE_INVALID,
            "defect": SampleDefect.INVALID,
            "provider_error": AIProviderNetworkError(),
        },
        {
            "reason": AssessmentFailureReason.CURATION_MISSING,
            "provider_error": AIProviderNetworkError(),
        },
        {
            "reason": AssessmentFailureReason.CURATION_MISSING,
            "defect": SampleDefect.INVALID,
        },
    ],
)
def test_rejects_invalid_detail_combinations(kwargs):
    with pytest.raises(TypeError):
        AssessmentError(**kwargs)


def test_response_invalid_preserves_defect():
    exc = AssessmentResponseInvalidError(SampleDefect.INVALID)
    assert exc.reason is AssessmentFailureReason.RESPONSE_INVALID
    assert exc.defect is SampleDefect.INVALID
    assert exc.code == SampleDefect.INVALID.value
    assert exc.provider_error is None
    assert str(exc) == "AssessmentResponseInvalidError(code='assessment_test_invalid')"


def test_response_invalid_rejects_free_text():
    with pytest.raises(TypeError):
        AssessmentResponseInvalidError("private response")


def test_curation_missing_has_no_details():
    exc = AssessmentCurationMissingError()
    assert exc.reason is AssessmentFailureReason.CURATION_MISSING
    assert exc.code == "assessment_curation_missing"
    assert exc.provider_error is None
    assert exc.defect is None


@pytest.mark.parametrize(
    "exc",
    [
        to_assessment_error(AIProviderNetworkError("private sdk text")),
        AssessmentResponseInvalidError(SampleDefect.INVALID),
        AssessmentCurationMissingError(),
    ],
)
def test_core_errors_are_independent_of_task_classification(exc):
    assert isinstance(exc, AssessmentError)
    assert not isinstance(exc, AssessmentTaskError)
    for attr in ("RETRYABILITY", "FAILURE_ACTION", "failure_kind", "failure_reason"):
        assert not hasattr(exc, attr)
    assert exc.SAFE_ATTRS == ("code",)
    assert "private sdk text" not in str(exc)
