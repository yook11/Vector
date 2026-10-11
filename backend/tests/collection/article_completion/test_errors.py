"""DB・外部通信を使わず、工程例外が原因の型と元の例外チェーンを保つことを確認する。"""

from datetime import UTC, datetime

from app.audit.error_chain import extract_error_chain
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
from app.http.errors import HttpResponseError


def test_completion_error_keeps_http_cause_and_retry_after() -> None:
    """工程例外はHTTP原因を同じオブジェクトのまま保持して待機指示を失わない。"""
    cause = HttpResponseError(
        status_code=429,
        received_at=datetime(2026, 10, 11, tzinfo=UTC),
        retry_after="120",
    )
    error = ArticleCompletionError(cause=cause)
    assert error.cause is cause
    assert error.__cause__ is cause
    assert error.cause.retry_after == "120"


def test_completion_error_preserves_extraction_exception_chain() -> None:
    """工程で包んでも抽出処理が保持した元例外とトレースバックを失わない。"""
    original = ValueError("parser failure")
    try:
        raise HtmlExtractionError(
            reason=HtmlExtractionFailureReason.EXTRACTOR_RAISED,
            message="記事の抽出器を実行中に例外が発生しました",
        ) from original
    except HtmlExtractionError as cause:
        traceback = cause.__traceback__
        error = ArticleCompletionError(cause=cause)
        assert error.cause is cause
        assert error.cause.__cause__ is original
        assert error.cause.__traceback__ is traceback
        assert extract_error_chain(error) == [
            "app.collection.article_completion.errors.ArticleCompletionError",
            "app.collection.article_completion.html_extraction.HtmlExtractionError",
            "builtins.ValueError",
        ]


def test_build_rejected_error_keeps_domain_rejection() -> None:
    """構築拒否はドメインで確定した理由を値のまま保持する。"""
    rejection = QualityTooLow(defects=(AnalyzableArticleDefect.PUBLISHED_AT_MISSING,))
    cause = ArticleBuildRejectedError(rejection)
    error = ArticleCompletionError(cause=cause)
    assert error.cause is cause
    assert cause.rejection is rejection
