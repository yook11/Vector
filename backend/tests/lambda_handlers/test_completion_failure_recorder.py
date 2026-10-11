"""DB・外部通信を使わず、補完の診断がJSONログへ理由と原因の型だけを出すことを確認する。"""

import io
import json

import structlog

from app.collection.article_acquisition.events import IncompleteArticleRecordedEvent
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
from app.lambda_handlers.completion.failure_recorder import (
    CompletionLambdaFailureRecorder,
)
from tests.collection.test_incomplete_article_recorded_event import valid_event


def record_result(result: RetryCompletion | NoRetryCompletion) -> dict:
    """実際のJSONレンダラーで配送結果の診断を一件出力する。"""
    output = io.StringIO()
    logger = structlog.wrap_logger(
        structlog.WriteLogger(output),
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    CompletionLambdaFailureRecorder(logger).record_completion(
        result,
        message_id="message",
        article_event=IncompleteArticleRecordedEvent.from_input(valid_event()),
    )
    return json.loads(output.getvalue())


def test_html_input_rejection_log_keeps_content_type_and_reason() -> None:
    """HTML以外による終了ログは抽出段階・理由・Content-Typeを保持する。"""
    cause = HtmlExtractionError(
        reason=HtmlExtractionFailureReason.CONTENT_TYPE_UNSUPPORTED,
        message="応答をHTMLとして扱えません",
        details={"content_type": "application/pdf"},
    )

    logged = record_result(
        NoRetryCompletion(
            cause=ArticleCompletionError(cause=cause), code="content_type_unsupported"
        )
    )

    assert logged == {
        "event": "completion_message_processed",
        "message_id": "message",
        "event_id": valid_event()["event_id"],
        "incomplete_article_id": 101,
        "result": "closed",
        "code": "content_type_unsupported",
        "requires_investigation": False,
        "failure_kind": "html_extraction",
        "reason_code": "content_type_unsupported",
        "content_type": "application/pdf",
        "error_chain": [
            "app.collection.article_completion.errors.ArticleCompletionError",
            "app.collection.article_completion.html_extraction.HtmlExtractionError",
        ],
    }


def test_extraction_retry_log_keeps_cause_types_without_exception_text() -> None:
    """抽出失敗のログは元例外の型を保持し、自由文や未選択の診断情報を出さない。"""
    original = ValueError("private-article-body")
    try:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.EXTRACTOR_RAISED,
            message="private-exception-message",
            details={"body": "private-diagnostic-body"},
        ) from original
    except HtmlExtractionError as cause:
        result = RetryCompletion(
            error=ArticleCompletionError(cause=cause),
            code="extraction_failed",
            requires_investigation=True,
        )

    logged = record_result(result)

    assert logged == {
        "event": "completion_message_processed",
        "message_id": "message",
        "event_id": valid_event()["event_id"],
        "incomplete_article_id": 101,
        "result": "retry",
        "code": "extraction_failed",
        "retry_at": None,
        "requires_investigation": True,
        "failure_kind": "html_extraction",
        "reason_code": "extraction_failed",
        "error_chain": [
            "app.collection.article_completion.errors.ArticleCompletionError",
            "app.collection.article_completion.html_extraction.HtmlExtractionError",
            "builtins.ValueError",
        ],
    }


def test_build_rejection_log_keeps_all_domain_defects() -> None:
    """構築拒否のログは抽出失敗と区別し、完成条件の不足理由をすべて保持する。"""
    cause = ArticleBuildRejectedError(
        QualityTooLow(
            defects=(
                AnalyzableArticleDefect.BODY_TOO_SHORT,
                AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
            )
        )
    )

    logged = record_result(
        NoRetryCompletion(
            cause=ArticleCompletionError(cause=cause), code="article_invalid"
        )
    )

    assert logged["failure_kind"] == "article_build"
    assert logged["defects"] == [
        "analyzable_article_body_too_short",
        "analyzable_article_published_at_missing",
    ]
    assert "reason_code" not in logged
    assert logged["error_chain"] == [
        "app.collection.article_completion.errors.ArticleCompletionError",
        "app.collection.article_completion.errors.ArticleBuildRejectedError",
    ]
