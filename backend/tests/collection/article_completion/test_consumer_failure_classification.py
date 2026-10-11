"""DB・外部通信を使わず、補完の失敗から再試行の要否と原因の保持を確認する。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.collection.article_completion.consumer_failure_classification import (
    classify_completion_failure,
)
from app.collection.article_completion.consumer_result import (
    NoRetryCompletion,
    RetryCompletion,
)
from app.collection.article_completion.errors import (
    ArticleCompletionRejectedError,
    ArticleContentQualityError,
    ArticleContentTypeError,
    ArticleExtractionCrashedError,
    ArticleExtractionCrashReason,
    ArticleExtractionEmptyError,
    FetchDeadlineExceededError,
    FetchResource,
    ResponseSizeLimitExceededError,
    RobotsDisallowedError,
)
from app.collection.domain.analyzable_article import AnalyzableArticleDefect as Defect
from app.collection.response_size import ResponseSizeBasis
from app.collection.retry_at import RetryAt
from app.http.destination_policy import HostBlockedError
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
            HostBlockedError(),
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
    result = classify_completion_failure(exc, now=_NOW)

    assert isinstance(result, expected_type)
    assert result.code == code
    assert result.requires_investigation is investigate
    if isinstance(result, RetryCompletion):
        assert result.error is exc
        assert result.retry_at == retry_at
    else:
        assert result.cause is exc


@pytest.mark.parametrize(
    "exc",
    [
        RobotsDisallowedError(),
        ResponseSizeLimitExceededError(
            resource=FetchResource.ARTICLE_PAGE,
            limit_bytes=1024,
            observed_bytes=1025,
            size_basis=ResponseSizeBasis.RECEIVED_DECODED_BODY,
        ),
        ArticleContentTypeError(content_type="application/json"),
        ArticleExtractionEmptyError(),
        ArticleContentQualityError(body_length=0, title_present=False),
        ArticleCompletionRejectedError(defects=(Defect.BODY_TOO_SHORT,)),
    ],
)
def test_known_completion_rejection_closes(exc: Exception) -> None:
    """確認済みの取得禁止や補完条件不足だけを終了へ進める。"""
    decision = classify_completion_failure(exc, now=_NOW)
    assert isinstance(decision, NoRetryCompletion)
    assert decision.cause is exc
    assert decision.requires_investigation is False


@pytest.mark.parametrize(
    ("defects", "unmapped"),
    [
        ((Defect.BODY_TOO_SHORT, Defect.UNMAPPED_VALIDATION_ERROR), ()),
        ((Defect.BODY_TOO_SHORT,), ("source_url:new_constraint",)),
        ((), ()),
    ],
)
def test_uncertain_build_rejection_preserves_reasons_and_retries(
    defects: tuple[Defect, ...], unmapped: tuple[str, ...]
) -> None:
    """未分類が混在する構築拒否は情報を失わず、終了より調査と再試行を優先する。"""
    exc = ArticleCompletionRejectedError(defects=defects, unmapped=unmapped)
    decision = classify_completion_failure(exc, now=_NOW)
    assert isinstance(decision, RetryCompletion)
    assert decision.error is exc
    assert decision.requires_investigation is True
    assert decision.code == "article_completion_rejected"
    assert exc.defects is defects
    assert exc.unmapped is unmapped


@pytest.mark.parametrize(
    ("exc", "code", "investigate"),
    [
        (
            FetchDeadlineExceededError(
                resource=FetchResource.ROBOTS_TXT, limit_seconds=10
            ),
            "article_fetch_deadline_exceeded",
            False,
        ),
        (
            ArticleExtractionCrashedError(
                reason=ArticleExtractionCrashReason.EXCEPTION
            ),
            "article_extraction_crashed",
            True,
        ),
        (
            ArticleExtractionCrashedError(
                reason=ArticleExtractionCrashReason.UNEXPECTED_RESULT
            ),
            "article_extraction_crashed",
            True,
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
        decision = classify_completion_failure(original, now=_NOW)
        assert isinstance(decision, RetryCompletion)
        assert decision.error is original
        assert decision.code == code
        assert decision.requires_investigation is investigate
        assert original.__cause__ is cause
        assert original.__traceback__ is traceback
