"""Assessmentの失敗理由・詳細の組み合わせと観測コードを検証する。"""

from enum import StrEnum

import pytest

from app.analysis.assessment.errors import (
    AssessmentCurationMissingError,
    AssessmentError,
    AssessmentFailureReason,
    AssessmentResponseInvalidError,
)
from app.shared.errors import ApplicationError


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
        {"reason": AssessmentFailureReason.RESPONSE_INVALID},
        {
            "reason": AssessmentFailureReason.RESPONSE_INVALID,
            "defect": "private response",
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


def test_response_invalid_rejects_free_text():
    with pytest.raises(TypeError):
        AssessmentResponseInvalidError("private response")


def test_curation_missing_has_no_details():
    exc = AssessmentCurationMissingError()
    assert exc.reason is AssessmentFailureReason.CURATION_MISSING
    assert exc.code == "assessment_curation_missing"
    assert exc.defect is None


@pytest.mark.parametrize(
    "exc",
    [
        AssessmentResponseInvalidError(SampleDefect.INVALID),
        AssessmentCurationMissingError(),
    ],
)
def test_core_errors_are_independent_of_task_classification(exc):
    assert isinstance(exc, AssessmentError)
    for attr in ("RETRYABILITY", "FAILURE_ACTION", "failure_kind", "failure_reason"):
        assert not hasattr(exc, attr)


def test_assessment_error_is_application_error():
    """工程例外を共通のアプリケーション例外として扱える。"""
    assert isinstance(AssessmentCurationMissingError(), ApplicationError)


@pytest.mark.parametrize(
    "error,expected_message",
    [
        (
            AssessmentResponseInvalidError(SampleDefect.INVALID),
            "AI応答が記事判定の契約を満たしていません",
        ),
        (AssessmentCurationMissingError(), "判定対象のCurationが存在しません"),
    ],
)
def test_assessment_error_describes_failure(error, expected_message):
    """工程の失敗を固定文で説明する。"""
    assert str(error) == expected_message


def test_response_invalid_is_assessment_error():
    """応答不正を工程例外として捕捉できる。"""
    assert issubclass(AssessmentResponseInvalidError, AssessmentError)
