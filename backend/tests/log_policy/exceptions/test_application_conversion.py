"""アプリで定義した例外の診断情報を、共通のログ形式へ変換する契約。"""

from dataclasses import asdict

import pytest
from pydantic import BaseModel, ValidationError

from app.ai_providers.deepseek.error_translator import DeepSeekStateReason
from app.ai_providers.errors import AIProviderError, AIProviderNetworkError
from app.analysis.assessment import events as assessment
from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.errors import (
    AssessmentCurationMissingError,
    AssessmentResponseInvalidError,
    to_assessment_error,
)
from app.analysis.curation import events as curation
from app.collection import events as collection
from app.collection.article_acquisition import events as acquisition
from app.log_policy.exceptions.conversion import convert_exception

pytestmark = pytest.mark.unit


def test_application_error_subclass_keeps_message_and_details() -> None:
    """ApplicationErrorを継承した例外のメッセージと診断情報を、共通形式へ写す。"""
    from app.shared.errors import ApplicationError

    class SampleApplicationError(ApplicationError):
        pass

    error = SampleApplicationError(
        "処理を実行できません",
        details={"reason": "missing_required_field"},
    )

    converted_error = convert_exception(error)

    assert converted_error.message == "処理を実行できません"
    assert converted_error.error_details == {"reason": "missing_required_field"}


def test_analyzable_event_keeps_reason_and_issues() -> None:
    """分析可能イベントの診断を、順序と重複を変えず任意属性を含めない形式へ写す。"""
    missing = collection.AnalyzableEventInvalidIssue(
        collection.AnalyzableEventInvalidField.ANALYZABLE_ARTICLE_ID,
        collection.AnalyzableEventInvalidCode.MISSING_REQUIRED_FIELD,
    )
    unknown = collection.AnalyzableEventInvalidIssue(
        collection.AnalyzableEventInvalidField.PAYLOAD,
        collection.AnalyzableEventInvalidCode.UNKNOWN_FIELD,
    )
    exc = collection.AnalyzableEventInvalidError(
        collection.AnalyzableEventInvalid(
            collection.AnalyzableEventInvalidReason.INVALID_PAYLOAD,
            (missing, unknown, missing),
        )
    )
    exc.extra = "synthetic-private-value"

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "Validation failed: invalid_payload",
        "error_details": {
            "kind": "application_validation",
            "reason": "invalid_payload",
            "issues": [
                {
                    "field": "payload.analyzable_article_id",
                    "code": "missing_required_field",
                },
                {"field": "payload", "code": "unknown_field"},
                {
                    "field": "payload.analyzable_article_id",
                    "code": "missing_required_field",
                },
            ],
        },
        "cause_is_aggregated": False,
    }


def test_incomplete_article_event_keeps_reason_and_issues() -> None:
    """未完成記事イベントの理由と各項目の違反をログの診断形式へ写す。"""
    exc = acquisition.IncompleteArticleEventInvalidError(
        acquisition.IncompleteArticleEventInvalid(
            acquisition.IncompleteArticleEventInvalidReason.INVALID_PAYLOAD,
            (
                acquisition.IncompleteArticleEventInvalidIssue(
                    acquisition.IncompleteArticleEventInvalidField.SOURCE_ID,
                    acquisition.IncompleteArticleEventInvalidCode.INVALID_TYPE,
                ),
                acquisition.IncompleteArticleEventInvalidIssue(
                    acquisition.IncompleteArticleEventInvalidField.INCOMPLETE_ARTICLE_ID,
                    acquisition.IncompleteArticleEventInvalidCode.MISSING_REQUIRED_FIELD,
                ),
            ),
        )
    )

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "Validation failed: invalid_payload",
        "error_details": {
            "kind": "application_validation",
            "reason": "invalid_payload",
            "issues": [
                {"field": "payload.source_id", "code": "invalid_type"},
                {
                    "field": "payload.incomplete_article_id",
                    "code": "missing_required_field",
                },
            ],
        },
        "cause_is_aggregated": False,
    }


def test_curated_event_keeps_reason_and_issues() -> None:
    """整形済みイベントの理由と各項目の違反をログの診断形式へ写す。"""
    exc = curation.CuratedEventInvalidError(
        curation.CuratedEventInvalid(
            curation.CuratedEventInvalidReason.INVALID_PAYLOAD,
            (
                curation.CuratedEventInvalidIssue(
                    curation.CuratedEventInvalidField.CURATION_ID,
                    curation.CuratedEventInvalidCode.INVALID_VALUE,
                ),
                curation.CuratedEventInvalidIssue(
                    curation.CuratedEventInvalidField.ANALYZABLE_ARTICLE_ID,
                    curation.CuratedEventInvalidCode.MISSING_REQUIRED_FIELD,
                ),
            ),
        )
    )

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "Validation failed: invalid_payload",
        "error_details": {
            "kind": "application_validation",
            "reason": "invalid_payload",
            "issues": [
                {"field": "payload.curation_id", "code": "invalid_value"},
                {
                    "field": "payload.analyzable_article_id",
                    "code": "missing_required_field",
                },
            ],
        },
        "cause_is_aggregated": False,
    }


def test_assessed_event_keeps_reason_and_issues() -> None:
    """評価済みイベントの理由と各項目の違反をログの診断形式へ写す。"""
    exc = assessment.AssessedEventInvalidError(
        assessment.AssessedEventInvalid(
            assessment.AssessedEventInvalidReason.UNSUPPORTED_SCHEMA_VERSION,
            (
                assessment.AssessedEventInvalidIssue(
                    assessment.AssessedEventInvalidField.SCHEMA_VERSION,
                    assessment.AssessedEventInvalidCode.UNSUPPORTED_SCHEMA_VERSION,
                ),
                assessment.AssessedEventInvalidIssue(
                    assessment.AssessedEventInvalidField.ANALYZED_ARTICLE_ID,
                    assessment.AssessedEventInvalidCode.MISSING_REQUIRED_FIELD,
                ),
            ),
        )
    )

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "Validation failed: unsupported_schema_version",
        "error_details": {
            "kind": "application_validation",
            "reason": "unsupported_schema_version",
            "issues": [
                {"field": "schema_version", "code": "unsupported_schema_version"},
                {
                    "field": "payload.analyzed_article_id",
                    "code": "missing_required_field",
                },
            ],
        },
        "cause_is_aggregated": False,
    }


def test_unknown_exception_does_not_read_invalid_attribute() -> None:
    """同名属性があっても未対応の例外は通常の例外として変換する。"""

    class UnknownError(Exception):
        @property
        def invalid(self):
            pytest.fail("must not inspect an unsupported exception")

    result = convert_exception(UnknownError("operation failed"))

    assert asdict(result) == {
        "message": "operation failed",
        "error_details": None,
        "cause_is_aggregated": False,
    }


def test_raw_validation_error_uses_existing_protected_conversion() -> None:
    """未変換のPydantic例外は既存の保護を通し、入力値や位置を出さない。"""

    class Payload(BaseModel):
        values: dict[str, int]

    with pytest.raises(ValidationError) as caught:
        Payload.model_validate({"values": {"synthetic-private-key": "private-value"}})

    result = convert_exception(caught.value)

    assert asdict(result) == {
        "message": "Validation failed (1 errors): int_parsing",
        "error_details": None,
        "cause_is_aggregated": False,
    }


def test_failed_diagnostic_access_does_not_stringify_exception() -> None:
    """診断属性を取得できなくても、原文へ戻らず固定メッセージを返す。"""

    class BrokenError(assessment.AssessedEventInvalidError):
        def __str__(self) -> str:
            pytest.fail("must not stringify the validation exception")

    exc = BrokenError(
        assessment.AssessedEventInvalid(
            assessment.AssessedEventInvalidReason.INVALID_PAYLOAD, ()
        )
    )
    del exc.invalid

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "[exception message unavailable]",
        "error_details": None,
        "cause_is_aggregated": False,
    }


def test_invalid_issue_does_not_return_partial_diagnostics() -> None:
    """項目の変換に失敗した場合は、不完全な診断や元の説明文を出さない。"""
    exc = assessment.AssessedEventInvalidError(
        assessment.AssessedEventInvalid(
            assessment.AssessedEventInvalidReason.INVALID_PAYLOAD,
            (object(),),
        )
    )
    exc.args = ("synthetic-private-message",)

    result = convert_exception(exc)

    assert asdict(result) == {
        "message": "[exception message unavailable]",
        "error_details": None,
        "cause_is_aggregated": False,
    }


def test_provider_error_keeps_message_code_and_reason() -> None:
    """プロバイダー例外の明示した診断だけを共通形式へ渡す。"""
    error = AIProviderNetworkError("request failed", reason=DeepSeekStateReason.TIMEOUT)
    error.response = {"body": "private-response"}

    converted = convert_exception(error)

    assert converted.message == "request failed"
    assert converted.error_details == {"code": "ai_error_network", "reason": "timeout"}


def test_provider_error_without_reason_keeps_only_code() -> None:
    """具体的な理由がなければ、通信失敗の説明とcodeだけを渡す。"""
    converted = convert_exception(AIProviderNetworkError())

    assert converted.message == "AIプロバイダーとの通信に失敗しました"
    assert converted.error_details == {"code": "ai_error_network"}


def test_unclassified_provider_base_does_not_invent_code() -> None:
    """CODE未定義の基底例外も変換でき、存在しない診断を補完しない。"""
    converted = convert_exception(AIProviderError("unclassified"))

    assert converted.message == "unclassified"
    assert converted.error_details is None


def test_unclassified_provider_base_keeps_explicit_reason() -> None:
    """CODEを持たない例外でも明示した理由は失わない。"""
    converted = convert_exception(
        AIProviderError(reason=DeepSeekStateReason.CONNECTION)
    )

    assert converted.error_details == {"reason": "connection"}


def test_assessment_provider_error_keeps_stage_reason_and_code() -> None:
    """工程の診断にはプロバイダー例外の本文やオブジェクトを転記しない。"""
    provider_error = AIProviderNetworkError(
        "private-provider-response", reason=DeepSeekStateReason.TIMEOUT
    )
    error = to_assessment_error(provider_error)

    converted = convert_exception(error)

    assert (
        converted.message == "AIプロバイダーの処理失敗により記事を判定できませんでした"
    )
    assert converted.error_details == {
        "reason": "provider_error",
        "code": "ai_error_network",
    }


@pytest.mark.parametrize(
    "category,expected_message,expected_code",
    [
        (
            {"private": "input"},
            "AI応答のcategoryが文字列ではありません",
            "assessment_response_category_wrong_type",
        ),
        (
            "private-unknown-category",
            "AI応答のcategoryが定義済みの分類ではありません",
            "assessment_response_category_unknown_value",
        ),
    ],
)
def test_assessment_response_error_keeps_specific_violation(
    category, expected_message, expected_code
) -> None:
    """検知場所の具体的な説明と違反を取得し、外側の診断には入力値を含めない。"""
    with pytest.raises(AssessmentResponseInvalidError) as caught:
        parse_assessment(
            {"category": category, "investor_take": "private-take", "key_points": []}
        )
    error = caught.value
    error.raw_response = "private-response"

    converted = convert_exception(error)

    assert converted.message == expected_message
    assert converted.error_details == {
        "reason": "response_invalid",
        "code": expected_code,
    }


def test_assessment_missing_curation_keeps_reason_and_code() -> None:
    """Curation欠損も他のAssessment例外と同じ診断形式へ写す。"""
    converted = convert_exception(AssessmentCurationMissingError())

    assert converted.message == "判定対象のCurationが存在しません"
    assert converted.error_details == {
        "reason": "curation_missing",
        "code": "assessment_curation_missing",
    }
