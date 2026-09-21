"""アプリの検証例外を、境界で整理済みの理由と項目別診断へ変換する。"""

from app.analysis.assessment.events import AssessedEventInvalidError
from app.analysis.curation.events import CuratedEventInvalidError
from app.collection.article_acquisition.events import IncompleteArticleEventInvalidError
from app.collection.events import AnalyzableEventInvalidError
from app.log_policy.exceptions.conversion import (
    ApplicationValidationDetails,
    ApplicationValidationIssue,
    ConvertedException,
    convert_exception,
)


def convert_application_exception(exc: BaseException) -> ConvertedException:
    """対応する検証例外だけから診断を取得し、それ以外は共通の変換へ渡す。"""
    if not isinstance(
        exc,
        (
            AnalyzableEventInvalidError,
            IncompleteArticleEventInvalidError,
            CuratedEventInvalidError,
            AssessedEventInvalidError,
        ),
    ):
        return convert_exception(exc)

    try:
        invalid = exc.invalid
        issues: list[ApplicationValidationIssue] = []
        for issue in invalid.issues:
            issues.append({"field": issue.field.value, "code": issue.code.value})

        details: ApplicationValidationDetails = {
            "kind": "application_validation",
            "reason": invalid.reason.value,
            "issues": issues,
        }
        return ConvertedException(
            message=f"Validation failed: {invalid.reason.value}",
            error_details=details,
        )
    except Exception:
        return ConvertedException(message="[exception message unavailable]")
