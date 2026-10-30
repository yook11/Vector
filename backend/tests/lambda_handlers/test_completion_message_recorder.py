"""DB・外部通信を使わず、補完の失敗が実際のログ規則を通り、原因の説明と診断情報を出すことを確認する。"""

import json

import pytest

from app.collection.article_completion.consumer_result import (
    NoRetryCompletion,
    RetryCompletion,
)
from app.collection.article_completion.errors import (
    ArticleBuildRejectedError,
    ArticleCompletionError,
)
from app.collection.article_completion.html_extraction import (
    HtmlExtractionError,
    HtmlExtractionFailureReason,
)
from app.collection.domain.analyzable_article import (
    AnalyzableArticleDefect,
    QualityTooLow,
)
from app.lambda_handlers.completion.message_recorder import CompletionMessageRecorder
from app.log_policy.policies.article_fetch import ARTICLE_FETCH_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

pytestmark = pytest.mark.unit


def _without_frames(value: object) -> object:
    """実行位置で変わる発生位置を、関連する例外も含めて取り除く。"""
    if isinstance(value, dict):
        return {
            key: _without_frames(item) for key, item in value.items() if key != "frames"
        }
    if isinstance(value, list):
        return [_without_frames(item) for item in value]
    return value


def record_consumed(
    capsys: pytest.CaptureFixture[str], result: RetryCompletion | NoRetryCompletion
) -> object:
    """実際の外部取得のログ規則で補完の結果を1件出力し、時刻と発生位置を除いて返す。"""
    logger = create_policy_json_logger("article_fetch", ARTICLE_FETCH_LOG_RULES)
    CompletionMessageRecorder(logger).record_consumed(result, duration_ms=12.5)
    (line,) = capsys.readouterr().out.splitlines()
    logged = json.loads(line)
    del logged["timestamp"]
    return _without_frames(logged)


def test_html_input_rejection_log_keeps_content_type_and_reason(capsys) -> None:
    """HTML以外による終了は、抽出の理由とContent-Typeを原因の診断情報として記録する。"""
    cause = HtmlExtractionError(
        reason=HtmlExtractionFailureReason.CONTENT_TYPE_UNSUPPORTED,
        message="応答をHTMLとして扱えません",
        details={"content_type": "application/pdf"},
    )

    logged = record_consumed(
        capsys,
        NoRetryCompletion(
            cause=ArticleCompletionError(cause=cause), code="content_type_unsupported"
        ),
    )

    assert logged == {
        "event": "completion_message_processing_failed",
        "level": "warning",
        "log_policy": "article_fetch",
        "code": "content_type_unsupported",
        "requires_investigation": False,
        "duration_ms": 12.5,
        "message_disposition": "completed",
        "error_class": (
            "app.collection.article_completion.errors.ArticleCompletionError"
        ),
        "error_message": "記事の補完工程で失敗しました",
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": (
                        "app.collection.article_completion.html_extraction."
                        "HtmlExtractionError"
                    ),
                    "error_message": "応答をHTMLとして扱えません",
                    "error_details": {
                        "content_type": "application/pdf",
                        "reason": "content_type_unsupported",
                    },
                },
            }
        ],
    }


def test_extraction_retry_log_keeps_cause_chain_at_error_level(capsys) -> None:
    """調査が要る抽出失敗はerrorで記録し、抽出の理由と抽出器の元例外まで原因をたどれる。"""
    original = ValueError("parser failure")
    try:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.EXTRACTOR_RAISED,
            message="記事の抽出器を実行中に例外が発生しました",
        ) from original
    except HtmlExtractionError as cause:
        result = RetryCompletion(
            error=ArticleCompletionError(cause=cause),
            code="extraction_failed",
            requires_investigation=True,
        )

    logged = record_consumed(capsys, result)

    assert logged == {
        "event": "completion_message_processing_failed",
        "level": "error",
        "log_policy": "article_fetch",
        "code": "extraction_failed",
        "requires_investigation": True,
        "duration_ms": 12.5,
        "message_disposition": "batch_item_failure",
        "error_class": (
            "app.collection.article_completion.errors.ArticleCompletionError"
        ),
        "error_message": "記事の補完工程で失敗しました",
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": (
                        "app.collection.article_completion.html_extraction."
                        "HtmlExtractionError"
                    ),
                    "error_message": "記事の抽出器を実行中に例外が発生しました",
                    "error_details": {"reason": "extraction_failed"},
                },
            },
            {
                "parent": 0,
                "relation": "cause",
                "exception": {
                    "error_class": "builtins.ValueError",
                    "error_message": "parser failure",
                },
            },
        ],
    }


def test_build_rejection_log_keeps_all_domain_defects(capsys) -> None:
    """構築拒否は、完成条件の不足理由をすべて原因の診断情報として記録する。"""
    cause = ArticleBuildRejectedError(
        QualityTooLow(
            defects=(
                AnalyzableArticleDefect.BODY_TOO_SHORT,
                AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
            )
        )
    )

    logged = record_consumed(
        capsys,
        NoRetryCompletion(
            cause=ArticleCompletionError(cause=cause), code="article_invalid"
        ),
    )

    assert logged == {
        "event": "completion_message_processing_failed",
        "level": "warning",
        "log_policy": "article_fetch",
        "code": "article_invalid",
        "requires_investigation": False,
        "duration_ms": 12.5,
        "message_disposition": "completed",
        "error_class": (
            "app.collection.article_completion.errors.ArticleCompletionError"
        ),
        "error_message": "記事の補完工程で失敗しました",
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": (
                        "app.collection.article_completion.errors."
                        "ArticleBuildRejectedError"
                    ),
                    "error_message": "記事が分析に必要な条件を満たしません",
                    "error_details": {
                        "defects": [
                            "analyzable_article_body_too_short",
                            "analyzable_article_published_at_missing",
                        ]
                    },
                },
            }
        ],
    }
