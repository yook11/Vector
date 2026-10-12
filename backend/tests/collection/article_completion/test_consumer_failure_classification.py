"""DB・外部通信を使わず、補完の失敗から再試行の要否と原因の保持を確認する。"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.collection.article_completion.consumer_failure_classification import (
    classify_completion_failure,
)
from app.collection.article_completion.consumer_result import (
    NoRetryCompletion,
    RetryCompletion,
)
from app.collection.article_completion.errors import (
    ArticleBuildRejectedError,
    ArticleCompletionError,
    FetchDeadlineExceededError,
    FetchResource,
    ResponseSizeLimitExceededError,
    RobotsDisallowedError,
)
from app.collection.article_completion.html_extraction import (
    HtmlExtractionError,
    HtmlExtractionFailureReason,
)
from app.collection.domain.analyzable_article import AnalyzableArticleDefect as Defect
from app.collection.domain.analyzable_article import QualityTooLow
from app.collection.response_size import ResponseSizeBasis
from app.collection.retry_at import RetryAt
from app.http.destination_policy import HostBlockedError, HostBlockedReason
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
)
from app.http.failure import (
    HttpTransportFailureReason as Reason,
)
from app.http.failure import (
    HttpTransportStage as Stage,
)

_RECEIVED = datetime(2026, 9, 14, 12, tzinfo=UTC)
_NOW = _RECEIVED + timedelta(seconds=30)


@pytest.mark.parametrize(
    ("exc", "expected_type", "code", "retry_at", "investigate"),
    [
        pytest.param(
            HttpResponseError(
                status_code=429, received_at=_RECEIVED, retry_after="120"
            ),
            RetryCompletion,
            "http_response_error",
            RetryAt(_RECEIVED + timedelta(seconds=120)),
            False,
            id="retryable_with_retry_after",
        ),
        pytest.param(
            HttpResponseError(status_code=511, received_at=_RECEIVED),
            RetryCompletion,
            "http_response_error",
            None,
            True,
            id="retryable_response_requiring_investigation",
        ),
        pytest.param(
            HttpTransportError(
                failure=HttpTransportFailure(Stage.UNKNOWN, Reason.NETWORK_IO)
            ),
            RetryCompletion,
            "http_transport_error",
            None,
            True,
            id="retryable_transport_requiring_investigation",
        ),
        pytest.param(
            HttpResponseError(
                status_code=403, received_at=_RECEIVED, retry_after="120"
            ),
            NoRetryCompletion,
            "http_response_error",
            None,
            False,
            id="non_retryable_response",
        ),
        pytest.param(
            HostBlockedError(
                reason=HostBlockedReason.NON_PUBLIC_IP_LITERAL,
                host="10.0.0.1",
                address="10.0.0.1",
            ),
            NoRetryCompletion,
            "host_blocked",
            None,
            False,
            id="host_blocked",
        ),
    ],
)
def test_external_fetch_judgement_becomes_completion_decision(
    exc: Exception,
    expected_type: type[RetryCompletion] | type[NoRetryCompletion],
    code: str,
    retry_at: RetryAt | None,
    investigate: bool,
) -> None:
    """外部取得の判断を、元例外・コード・待機時刻・調査対象を保って補完の結果へ写す。"""
    error = ArticleCompletionError(cause=exc)
    result = classify_completion_failure(error, now=_NOW)

    assert isinstance(result, expected_type)
    assert result.code == code
    assert result.requires_investigation is investigate
    if isinstance(result, RetryCompletion):
        assert result.error is error
        assert result.retry_at == retry_at
    else:
        assert result.cause is error


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (RobotsDisallowedError(), "robots_disallowed"),
        (
            ResponseSizeLimitExceededError(
                resource=FetchResource.ARTICLE_PAGE,
                limit_bytes=1024,
                observed_bytes=1025,
                size_basis=ResponseSizeBasis.RECEIVED_DECODED_BODY,
            ),
            "response_too_large",
        ),
        (
            ArticleBuildRejectedError(QualityTooLow(defects=(Defect.BODY_TOO_SHORT,))),
            "article_invalid",
        ),
    ],
)
def test_known_completion_rejection_closes(exc: Exception, code: str) -> None:
    """確認済みの取得禁止や記事の条件不足は、原因の型から終了へ進める。"""
    error = ArticleCompletionError(cause=exc)
    decision = classify_completion_failure(error, now=_NOW)
    assert decision == NoRetryCompletion(cause=error, code=code)


def test_unclassified_validation_error_requires_investigation() -> None:
    """未分類のValidationErrorは記事の構築拒否と区別し、原因を保って再試行する。"""
    cause = ValidationError.from_exception_data(
        "AnalyzableArticle", [{"type": "missing", "loc": ("new_field",), "input": {}}]
    )
    error = ArticleCompletionError(cause=cause)
    assert classify_completion_failure(error, now=_NOW) == RetryCompletion(
        error=error, code="validation_error", requires_investigation=True
    )
    assert error.cause is cause


@pytest.mark.parametrize(
    ("exc", "code", "investigate"),
    [
        (
            FetchDeadlineExceededError(
                resource=FetchResource.ROBOTS_TXT, limit_seconds=10
            ),
            "fetch_deadline_exceeded",
            False,
        ),
        (TypeError("unexpected implementation failure"), "unknown", True),
    ],
)
def test_execution_failure_preserves_cause_and_retries(
    exc: Exception, code: str, investigate: bool
) -> None:
    """実行障害を再試行へ渡し、調査に必要な元の例外チェーンを保持する。"""
    cause = ValueError("original failure")
    try:
        raise exc from cause
    except Exception as original:
        traceback = original.__traceback__
        error = ArticleCompletionError(cause=original)
        decision = classify_completion_failure(error, now=_NOW)
        assert isinstance(decision, RetryCompletion)
        assert decision.error is error
        assert decision.error.cause is original
        assert decision.code == code
        assert decision.requires_investigation is investigate
        assert original.__cause__ is cause
        assert original.__traceback__ is traceback


def test_unsupported_content_type_finishes_completion() -> None:
    """HTML以外の応答による抽出失敗は原因を保持して終了する。"""
    error = ArticleCompletionError(
        cause=HtmlExtractionError(
            reason=HtmlExtractionFailureReason.CONTENT_TYPE_UNSUPPORTED,
            message="HTMLではありません",
            details={"content_type": "application/pdf"},
        )
    )
    assert classify_completion_failure(error, now=_NOW) == NoRetryCompletion(
        cause=error, code="content_type_unsupported"
    )


def test_empty_extraction_finishes_completion() -> None:
    """抽出結果なしは記事の素材を得られなかった理由を保持して終了する。"""
    error = ArticleCompletionError(
        cause=HtmlExtractionError(
            reason=HtmlExtractionFailureReason.RESULT_EMPTY,
            message="素材が得られませんでした",
        )
    )
    assert classify_completion_failure(error, now=_NOW) == NoRetryCompletion(
        cause=error, code="extraction_empty"
    )


def test_extractor_exception_retries_completion() -> None:
    """抽出器の実行失敗は原因を保持して調査対象の再試行にする。"""
    error = ArticleCompletionError(
        cause=HtmlExtractionError(
            reason=HtmlExtractionFailureReason.EXTRACTOR_RAISED,
            message="抽出器で例外が発生しました",
        )
    )
    assert classify_completion_failure(error, now=_NOW) == RetryCompletion(
        error=error, code="extraction_failed", requires_investigation=True
    )


def test_extraction_result_type_mismatch_retries_completion() -> None:
    """抽出器の戻り値が契約と違う場合は調査対象の再試行にする。"""
    error = ArticleCompletionError(
        cause=HtmlExtractionError(
            reason=HtmlExtractionFailureReason.RESULT_TYPE_MISMATCH,
            message="Document型ではありません",
        )
    )
    assert classify_completion_failure(error, now=_NOW) == RetryCompletion(
        error=error, code="extraction_result_unexpected", requires_investigation=True
    )


def test_html_decode_failure_retries_completion() -> None:
    """HTMLのデコード失敗は発生箇所を保持して調査対象の再試行にする。"""
    error = ArticleCompletionError(
        cause=HtmlExtractionError(
            reason=HtmlExtractionFailureReason.HTML_DECODE_FAILED,
            message="HTMLのデコードに失敗しました",
        )
    )
    assert classify_completion_failure(error, now=_NOW) == RetryCompletion(
        error=error, code="html_decode_failed", requires_investigation=True
    )


def test_extraction_material_conversion_failure_retries_completion() -> None:
    """抽出素材の整形失敗は発生箇所を保持して調査対象の再試行にする。"""
    error = ArticleCompletionError(
        cause=HtmlExtractionError(
            reason=HtmlExtractionFailureReason.MATERIAL_CONVERSION_FAILED,
            message="抽出素材の整形に失敗しました",
        )
    )
    assert classify_completion_failure(error, now=_NOW) == RetryCompletion(
        error=error,
        code="extraction_material_conversion_failed",
        requires_investigation=True,
    )
